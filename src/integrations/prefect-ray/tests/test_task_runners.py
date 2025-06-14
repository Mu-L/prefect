import asyncio
import logging
import random
import subprocess
import sys
import time
import warnings

import pytest
import ray
import ray.cluster_utils
from prefect_ray import RayTaskRunner
from prefect_ray.context import remote_options

import prefect
import prefect.task_engine
import tests
from prefect import flow, task
from prefect.assets import Asset, materialize
from prefect.client.orchestration import get_client
from prefect.context import get_run_context
from prefect.futures import as_completed
from prefect.states import State, StateType
from prefect.testing.fixtures import (  # noqa: F401
    hosted_api_server,
    use_hosted_api_server,
)


@pytest.fixture(scope="session")
def event_loop(request):
    """
    Redefine the event loop to support session/module-scoped fixtures;
    see https://github.com/pytest-dev/pytest-asyncio/issues/68
    When running on Windows we need to use a non-default loop for subprocess support.
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    policy = asyncio.get_event_loop_policy()

    loop = policy.new_event_loop()

    # configure asyncio logging to capture long running tasks
    asyncio_logger = logging.getLogger("asyncio")
    asyncio_logger.setLevel("WARNING")
    asyncio_logger.addHandler(logging.StreamHandler())
    loop.set_debug(True)
    loop.slow_callback_duration = 0.25

    try:
        yield loop
    finally:
        loop.close()


@pytest.fixture
def machine_ray_instance():
    """
    Starts a ray instance for the current machine
    """
    # First ensure any existing Ray processes are stopped
    try:
        subprocess.run(
            ["ray", "stop"],
            check=True,
            capture_output=True,
            cwd=str(prefect.__development_base_path__),
        )
    except subprocess.CalledProcessError:
        # It's okay if ray stop fails - it might not be running
        pass

    try:
        # Start Ray with clean session
        subprocess.check_output(
            [
                "ray",
                "start",
                "--head",
                "--include-dashboard",
                "False",
                "--disable-usage-stats",
            ],
            cwd=str(prefect.__development_base_path__),
        )
        yield "ray://127.0.0.1:10001"
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"Failed to start ray: {exc.stderr or exc}")
    finally:
        # Always try to stop Ray in the cleanup
        try:
            subprocess.run(
                ["ray", "stop"],
                check=True,
                capture_output=True,
                cwd=str(prefect.__development_base_path__),
            )
        except subprocess.CalledProcessError:
            pass  # Best effort cleanup


@pytest.fixture
def default_ray_task_runner():
    with warnings.catch_warnings():
        # Ray does not properly close resources and we do not want their warnings to
        # bubble into our test suite
        # https://github.com/ray-project/ray/pull/22419
        warnings.simplefilter("ignore", ResourceWarning)

        yield RayTaskRunner()


@pytest.fixture
def ray_task_runner_with_existing_cluster(
    machine_ray_instance,
    use_hosted_api_server,  # noqa: F811
    hosted_api_server,  # noqa: F811
):
    """
    Generate a ray task runner that's connected to a ray instance running in a separate
    process.

    This tests connection via `ray://` which is a client-based connection.
    """
    yield RayTaskRunner(
        address=machine_ray_instance,
        init_kwargs={
            "runtime_env": {
                # Ship the 'tests' module to the workers or they will not be able to
                # deserialize test tasks / flows
                "py_modules": [tests]
            }
        },
    )


@pytest.fixture
def inprocess_ray_cluster():
    """
    Starts a ray cluster in-process
    """
    cluster = ray.cluster_utils.Cluster(initialize_head=True)
    try:
        cluster.add_node()  # We need to add a second node for parallelism
        yield cluster
    finally:
        cluster.shutdown()


@pytest.fixture
def ray_task_runner_with_inprocess_cluster(
    inprocess_ray_cluster,
    use_hosted_api_server,  # noqa: F811
    hosted_api_server,  # noqa: F811
):
    """
    Generate a ray task runner that's connected to an in-process cluster.

    This tests connection via 'localhost' which is not a client-based connection.
    """

    yield RayTaskRunner(
        address=inprocess_ray_cluster.address,
        init_kwargs={
            "runtime_env": {
                # Ship the 'tests' module to the workers or they will not be able to
                # deserialize test tasks / flows
                "py_modules": [tests]
            }
        },
    )


@pytest.fixture
def ray_task_runner_with_temporary_cluster(
    use_hosted_api_server,  # noqa: F811
    hosted_api_server,  # noqa: F811
):
    """
    Generate a ray task runner that creates a temporary cluster.

    This tests connection via 'localhost' which is not a client-based connection.
    """

    yield RayTaskRunner(
        init_kwargs={
            "runtime_env": {
                # Ship the 'tests' module to the workers or they will not be able to
                # deserialize test tasks / flows
                "py_modules": [tests]
            }
        },
    )


task_runner_setups = [
    default_ray_task_runner,
    ray_task_runner_with_inprocess_cluster,
    ray_task_runner_with_temporary_cluster,
]

if sys.version_info >= (3, 10):
    task_runner_setups.append(ray_task_runner_with_existing_cluster)


class TestRayTaskRunner:
    @pytest.fixture(params=task_runner_setups)
    def task_runner(self, request):
        fixture_name = request.param._fixture_function.__name__
        yield request.getfixturevalue(fixture_name)

    @pytest.fixture
    def tmp_file(self, tmp_path):
        file_path = tmp_path / "canary.txt"
        file_path.touch()
        return file_path

    async def test_duplicate(self, task_runner):
        new = task_runner.duplicate()
        assert new == task_runner
        assert new is not task_runner

    async def test_successful_flow_run(self, task_runner):
        @task
        def task_a():
            return "a"

        @task
        def task_b():
            return "b"

        @task
        def task_c(b):
            return b + "c"

        @flow(version="test", task_runner=task_runner)
        def test_flow():
            a = task_a.submit()
            b = task_b.submit()
            c = task_c.submit(b)
            return a, b, c

        a, b, c = test_flow()
        assert await a.result() == "a"
        assert await b.result() == "b"
        assert await c.result() == "bc"

    async def test_failing_flow_run(self, task_runner):
        @task
        def task_a():
            raise RuntimeError("This task fails!")

        @task
        def task_b():
            raise ValueError("This task fails and passes data downstream!")

        @task
        def task_c(b):
            # This task attempts to use the upstream data and should fail too
            return b + "c"

        @flow(version="test", task_runner=task_runner)
        def test_flow():
            a = task_a.submit()
            b = task_b.submit()
            c = task_c.submit(b)
            d = task_c.submit(c)

            return a, b, c, d

        state = test_flow(return_state=True)

        assert state.is_failed()
        result = await state.result(raise_on_failure=False)
        a, b, c, d = result
        with pytest.raises(RuntimeError, match="This task fails!"):
            await a.result()
        with pytest.raises(
            ValueError, match="This task fails and passes data downstream"
        ):
            await b.result()

        assert c.is_pending()
        assert c.name == "NotReady"
        assert (
            f"Upstream task run '{b.state_details.task_run_id}' did not reach a"
            " 'COMPLETED' state" in c.message
        )

        assert d.is_pending()
        assert d.name == "NotReady"
        assert (
            f"Upstream task run '{c.state_details.task_run_id}' did not reach a"
            " 'COMPLETED' state" in d.message
        )

    async def test_async_tasks(self, task_runner):
        @task
        async def task_a():
            return "a"

        @task
        async def task_b():
            return "b"

        @task
        async def task_c(b):
            return b + "c"

        @flow(version="test", task_runner=task_runner)
        async def test_flow():
            a = task_a.submit()
            b = task_b.submit()
            c = task_c.submit(b)
            return a, b, c

        a, b, c = await test_flow()
        assert await a.result() == "a"
        assert await b.result() == "b"
        assert await c.result() == "bc"

    async def test_submit_and_wait(self, task_runner):
        @task
        async def task_a():
            return "a"

        async def fake_orchestrate_task_run(example_kwarg):
            return State(
                type=StateType.COMPLETED,
                data=example_kwarg,
            )

        with task_runner:
            future = task_runner.submit(task_a, parameters={}, wait_for=[])
            future.wait()
            state = future.state
            assert await state.result() == "a"

    async def test_async_task_timeout(self, task_runner):
        @task(timeout_seconds=0.1)
        async def my_timeout_task():
            await asyncio.sleep(2)
            return 42

        @task
        async def my_dependent_task(task_res):
            return 1764

        @task
        async def my_independent_task():
            return 74088

        @flow(version="test", task_runner=task_runner)
        async def test_flow():
            a = my_timeout_task.submit()
            b = my_dependent_task.submit(a)
            c = my_independent_task.submit()

            return a, b, c

        state = await test_flow(return_state=True)

        assert state.is_failed()
        ax, bx, cx = await state.result(raise_on_failure=False)
        assert ax.type == StateType.FAILED
        assert bx.type == StateType.PENDING
        assert cx.type == StateType.COMPLETED

    async def test_sync_task_timeout(self, task_runner):
        @task(timeout_seconds=1)
        def my_timeout_task():
            time.sleep(2)
            return 42

        @task
        def my_dependent_task(task_res):
            return 1764

        @task
        def my_independent_task():
            return 74088

        @flow(version="test", task_runner=task_runner)
        def test_flow():
            a = my_timeout_task.submit()
            b = my_dependent_task.submit(a)
            c = my_independent_task.submit()

            return a, b, c

        state = test_flow(return_state=True)

        assert state.is_failed()
        ax, bx, cx = await state.result(raise_on_failure=False)
        assert ax.type == StateType.FAILED
        assert bx.type == StateType.PENDING
        assert cx.type == StateType.COMPLETED

    def test_as_completed_yields_correct_order(self, task_runner):
        @task
        def task_a(seconds):
            time.sleep(seconds)
            return seconds

        timings = [1, 5, 10]

        @flow(version="test", task_runner=task_runner)
        def test_flow():
            done_futures = []
            futures = [task_a.submit(seconds) for seconds in reversed(timings)]
            for future in as_completed(futures=futures):
                done_futures.append(future.result())
            assert done_futures[-1] == timings[-1]

        test_flow()

    def get_sleep_time(self) -> float:
        """
        Return an amount of time to sleep for concurrency tests.
        The RayTaskRunner is prone to flaking on concurrency tests.
        """
        return 5.0

    async def test_wait_captures_exceptions_as_crashed_state(self, task_runner):
        """
        Ray wraps the exception, interrupts will result in "Cancelled" tasks
        or "Killed" workers while normal errors will result in a "RayTaskError".
        We care more about the crash detection and
        lack of re-raise here than the equality of the exception.
        """

        @task
        async def task_a():
            raise KeyboardInterrupt()

        with task_runner:
            future = task_runner.submit(
                task=task_a,
                parameters={},
                wait_for=[],
            )

            future.wait()
            state = future.state
            assert state is not None, "wait timed out"
            assert isinstance(state, State), "wait should return a state"
            assert state.name == "Crashed"

    def test_flow_and_subflow_both_with_task_runner(self, task_runner, tmp_file):
        @task
        def some_task(text):
            tmp_file.write_text(text)

        @flow(task_runner=RayTaskRunner())
        def subflow():
            a = some_task.submit("a")
            b = some_task.submit("b")
            c = some_task.submit("c")
            return a, b, c

        @flow(task_runner=task_runner)
        def base_flow():
            subflow()
            time.sleep(self.get_sleep_time())
            d = some_task.submit("d")
            return d

        base_flow()
        assert tmp_file.read_text() == "d"

    def test_ray_options(self):
        @task
        def process(x):
            return x + 1

        @flow(task_runner=RayTaskRunner())
        def my_flow():
            # equivalent to setting @ray.remote(max_calls=1)
            with remote_options(max_calls=1):
                process.submit(42)

        my_flow()

    def test_dependencies(self):
        @task
        def a():
            time.sleep(self.get_sleep_time())

        b = c = d = e = a

        @flow(task_runner=RayTaskRunner())
        def flow_with_dependent_tasks():
            for _ in range(3):
                a_future = a.submit(wait_for=[])
                b_future = b.submit(wait_for=[a_future])

                c.submit(wait_for=[b_future])
                d.submit(wait_for=[b_future])
                e.submit(wait_for=[b_future])

        flow_with_dependent_tasks()

    def test_can_run_many_tasks_without_crashing(self, task_runner):
        """
        Regression test for https://github.com/PrefectHQ/prefect/issues/15539
        """

        @task
        def random_integer(range_from: int = 0, range_to: int = 100) -> int:
            """Task that returns a random integer."""

            random_int = random.randint(range_from, range_to)

            return random_int

        @flow(task_runner=task_runner)
        def add_random_integers(number_tasks: int = 50) -> int:
            """Flow that submits some random_integer tasks and returns the sum of the results."""

            futures = []
            for _ in range(number_tasks):
                futures.append(random_integer.submit())

            sum = 0
            for future in futures:
                sum += future.result()

            return sum

        assert add_random_integers() > 0

    async def test_assets_with_task_runner(self, task_runner):
        upstream = Asset(key="s3://data/dask_raw")
        downstream = Asset(key="s3://data/dask_processed")

        @materialize(upstream)
        async def extract():
            return {"rows": 50}

        @materialize(downstream)
        async def load(d):
            return {"rows": d["rows"] * 2}

        @flow(version="test", task_runner=task_runner)
        async def pipeline():
            run_context = get_run_context()
            raw_data = extract.submit()
            processed = load.submit(raw_data)
            processed.wait()
            return run_context.flow_run.id

        flow_run_id = await pipeline()

        async with get_client() as client:
            for i in range(5):
                response = await client._client.post(
                    "/events/filter",
                    json={
                        "filter": {
                            "event": {"prefix": ["prefect.asset."]},
                            "related": {"id": [f"prefect.flow-run.{flow_run_id}"]},
                        },
                    },
                )
                response.raise_for_status()
                data = response.json()
                asset_events = data.get("events", [])
                if len(asset_events) >= 3:
                    break
                # give a little more time for
                # server to process events
                await asyncio.sleep(2)
            else:
                raise RuntimeError("Unable to get any events from server!")

        assert len(asset_events) == 3

        upstream_events = [
            e
            for e in asset_events
            if e.get("resource", {}).get("prefect.resource.id") == upstream.key
        ]
        downstream_events = [
            e
            for e in asset_events
            if e.get("resource", {}).get("prefect.resource.id") == downstream.key
        ]

        # Should have 2 events for upstream (1 materialization, 1 reference)
        assert len(upstream_events) == 2
        assert len(downstream_events) == 1

        # Separate upstream events by type
        upstream_mat_events = [
            e
            for e in upstream_events
            if e["event"] == "prefect.asset.materialization.succeeded"
        ]
        upstream_ref_events = [
            e for e in upstream_events if e["event"] == "prefect.asset.referenced"
        ]

        assert len(upstream_mat_events) == 1
        assert len(upstream_ref_events) == 1

        upstream_mat_event = upstream_mat_events[0]
        upstream_ref_event = upstream_ref_events[0]
        downstream_event = downstream_events[0]

        # confirm upstream materialization event
        assert upstream_mat_event["event"] == "prefect.asset.materialization.succeeded"
        assert upstream_mat_event["resource"]["prefect.resource.id"] == upstream.key

        # confirm upstream reference event
        assert upstream_ref_event["event"] == "prefect.asset.referenced"
        assert upstream_ref_event["resource"]["prefect.resource.id"] == upstream.key

        # confirm downstream events
        assert downstream_event["event"] == "prefect.asset.materialization.succeeded"
        assert downstream_event["resource"]["prefect.resource.id"] == downstream.key
        related_assets = [
            r
            for r in downstream_event["related"]
            if r.get("prefect.resource.role") == "asset"
        ]
        assert len(related_assets) == 1
        assert related_assets[0]["prefect.resource.id"] == upstream.key
