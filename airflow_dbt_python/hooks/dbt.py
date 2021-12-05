"""Provides a hook to interact with a dbt project."""
import dataclasses
import json
import os
import pickle
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Union

import dbt.flags as flags
from dbt.adapters.factory import register_adapter
from dbt.config.runtime import RuntimeConfig
from dbt.contracts.graph.manifest import Manifest
from dbt.contracts.results import RunResult
from dbt.exceptions import InternalException
from dbt.graph import Graph
from dbt.main import adapter_management, initialize_config_values, track_run
from dbt.task.base import BaseTask
from dbt.task.build import BuildTask
from dbt.task.clean import CleanTask
from dbt.task.compile import CompileTask
from dbt.task.debug import DebugTask
from dbt.task.deps import DepsTask
from dbt.task.freshness import FreshnessTask
from dbt.task.list import ListTask
from dbt.task.parse import ParseTask
from dbt.task.run import RunTask
from dbt.task.run_operation import RunOperationTask
from dbt.task.runnable import ManifestTask
from dbt.task.seed import SeedTask
from dbt.task.snapshot import SnapshotTask
from dbt.task.test import TestTask
from dbt.version import get_installed_version

from airflow.hooks.base_hook import BaseHook

DBT_VERSION = get_installed_version()
DBT_VERSION_STRING = DBT_VERSION.to_version_string()
DBT_VERSION_TUPLE = (int(DBT_VERSION.major), int(DBT_VERSION.minor))


class FromStrMixin(Enum):
    """Access enum variants with strings ensuring uppercase."""

    @classmethod
    def from_str(cls, s: str):
        """Instantiate an Enum from a string."""
        return cls[s.replace("-", "_").upper()]


class LogFormat(FromStrMixin, Enum):
    """Allowed dbt log formats."""

    DEFAULT = "default"
    JSON = "json"
    TEXT = "text"


class IndirectSelection(FromStrMixin, Enum):
    """Allowed indirect selection arguments."""

    EAGER = "eager"
    CAUTIOS = "cautios"


class Output(FromStrMixin, Enum):
    """Allowed output arguments."""

    JSON = "json"
    NAME = "name"
    PATH = "path"
    SELECTOR = "selector"

    def __eq__(self, other):
        """Override equality for string comparison."""
        if isinstance(other, str):
            return other.upper() == self.name
        return Enum.__eq__(self, other)


@dataclass
class BaseConfig:
    """BaseConfig dbt arguments for all tasks."""

    record_timing_info: Optional[str] = None
    debug: Optional[bool] = None
    bypass_cache: Optional[bool] = None
    log_format: Optional[LogFormat] = None
    warn_error: Optional[bool] = None
    use_experimental_parser: Optional[bool] = None
    no_static_parser: Optional[bool] = None
    no_anonymous_usage_stats: Optional[bool] = None
    partial_parse: Optional[bool] = None
    no_partial_parse: Optional[bool] = None
    use_colors: Optional[bool] = None
    no_use_colors: Optional[bool] = None
    no_version_check: Optional[bool] = None
    single_threaded: Optional[bool] = None
    fail_fast: Optional[bool] = None
    project_dir: Optional[str] = None
    profiles_dir: Optional[str] = None
    profile: Optional[str] = None
    target: Optional[str] = None
    vars: str = "{}"
    log_cache_events: Optional[bool] = None
    defer: Optional[bool] = None
    no_defer: Optional[bool] = None
    state: Optional[str] = None
    threads: Optional[int] = None
    compiled_target: Optional[Union[os.PathLike[str], str]] = None

    def __post_init__(self):
        """Support dictionary args by casting them to str after setting."""
        if isinstance(self.vars, dict):
            self.vars = json.dumps(self.vars)

    @property
    def dbt_task(self) -> BaseTask:
        """Access to the underlyingn dbt task class."""
        if getattr(self, "cls", None) is None:
            raise NotImplementedError()
        return getattr(self, "cls")

    def patch_manifest_task(self, task: BaseTask):
        """Patch a dbt task to use a pre-compiled graph and manifest.

        Parsing and compilation of a dbt project starts with the invocation of
        ManifestTask._runtime_initialize. Since GraphRunnableTask uses super()
        to invoke _runtime_initialize, we patch this method and avoid the super()
        call.

        Raises:
            TypeError: If the dbt task is not a subclass of ManifestTask.
        """
        if isinstance(task, ManifestTask) is False:
            raise TypeError(
                f"Patching requires an instance of ManifestTask, not {type(task)}"
            )

        if self.compiled_target is None:
            raise ValueError("Patching requires compiled_target to be defined.")

        graph_path = Path(self.compiled_target) / "graph.gpickle"
        manifest_path = Path(self.compiled_target) / "manifest.json"

        def _runtime_initialize():
            with open(graph_path, "rb") as f:
                task.graph = Graph(graph=pickle.load(f))

            with open(manifest_path) as f:
                loaded_manifest = json.load(f)
                # If I'm taking something from this experience, it's this Mashumaru
                # package. I spent a long time trying to build a manifest, when I only
                # had to call from_dict. Amazing stuff.
                Manifest.from_dict(loaded_manifest)
                task.manifest = Manifest.from_dict(loaded_manifest)

            # What follows is the remaining _runtime_initialize method of
            # GraphRunnableTask.
            task.job_queue = task.get_graph_queue()

            task._flattened_nodes = []
            for uid in task.job_queue.get_selected_nodes():
                if uid in task.manifest.nodes:
                    task._flattened_nodes.append(task.manifest.nodes[uid])
                elif uid in task.manifest.sources:
                    task._flattened_nodes.append(task.manifest.sources[uid])
                else:
                    raise InternalException(
                        f"Node selection returned {uid}, expected a node or a "
                        f"source"
                    )
            task.num_nodes = len(
                [n for n in task._flattened_nodes if not n.is_ephemeral_model]
            )

        task._runtime_initialize = _runtime_initialize

    def create_dbt_task(self) -> BaseTask:
        """Create a dbt task given with this configuration."""
        task = self.dbt_task.from_args(self)
        if (
            self.compiled_target is not None
            and issubclass(self.dbt_task, ManifestTask) is True
        ):
            # Only supported by subclasses of dbt's ManifestTask.
            # Represented here by the presence of the compiled_target attribute.
            self.patch_manifest_task(task)

        return task


@dataclass
class SelectionConfig(BaseConfig):
    """Node selection arguments for dbt tasks like run and seed."""

    exclude: Optional[list[str]] = None
    select: Optional[list[str]] = None
    selector_name: Optional[list[str]] = None
    # Kept for compatibility with dbt versions < 0.21
    models: Optional[list[str]] = None


@dataclass
class TableMutabilityConfig(SelectionConfig):
    """Specify whether tables should be dropped and recreated."""

    full_refresh: Optional[bool] = None


@dataclass
class BuildTaskConfig(TableMutabilityConfig):
    """Dbt build task arguments."""

    cls: BaseTask = dataclasses.field(default=BuildTask, init=False)
    compiled_target: Optional[Union[os.PathLike[str], str]] = None
    data: Optional[bool] = None
    indirect_selection: Optional[IndirectSelection] = None
    resource_types: Optional[list[str]] = None
    schema: Optional[bool] = None
    show: Optional[bool] = None
    store_failures: Optional[bool] = None
    which: str = dataclasses.field(default="build", init=False)


@dataclass
class CleanTaskConfig(BaseConfig):
    """Dbt clean task arguments."""

    cls: BaseTask = dataclasses.field(default=CleanTask, init=False)
    parse_only: Optional[bool] = None
    which: str = dataclasses.field(default="clean", init=False)


@dataclass
class CompileTaskConfig(TableMutabilityConfig):
    """Dbt compile task arguments."""

    cls: BaseTask = dataclasses.field(default=CompileTask, init=False)
    parse_only: Optional[bool] = None
    which: str = dataclasses.field(default="compile", init=False)


@dataclass
class DebugTaskConfig(BaseConfig):
    """Dbt debug task arguments."""

    cls: BaseTask = dataclasses.field(default=DebugTask, init=False)
    config_dir: Optional[bool] = None
    which: str = dataclasses.field(default="debug", init=False)


@dataclass
class DepsTaskConfig(BaseConfig):
    """Compile task arguments."""

    cls: BaseTask = dataclasses.field(default=DepsTask, init=False)
    which: str = dataclasses.field(default="deps", init=False)


@dataclass
class ListTaskConfig(SelectionConfig):
    """Dbt list task arguments."""

    cls: BaseTask = dataclasses.field(default=ListTask, init=False)
    compiled_target: Optional[Union[os.PathLike[str], str]] = None
    indirect_selection: Optional[IndirectSelection] = None
    output: Output = Output.SELECTOR
    output_keys: Optional[list[str]] = None
    resource_types: Optional[list[str]] = None
    which: str = dataclasses.field(default="list", init=False)


@dataclass
class ParseTaskConfig(BaseConfig):
    """Dbt parse task arguments."""

    cls: BaseTask = dataclasses.field(default=ParseTask, init=False)
    compile: Optional[bool] = None
    which: str = dataclasses.field(default="parse", init=False)
    write_manifest: Optional[bool] = None


@dataclass
class RunTaskConfig(TableMutabilityConfig):
    """Dbt run task arguments."""

    cls: BaseTask = dataclasses.field(default=RunTask, init=False)
    compiled_target: Optional[Union[os.PathLike[str], str]] = None
    which: str = dataclasses.field(default="run", init=False)


@dataclass
class RunOperationTaskConfig(BaseConfig):
    """Dbt run-operation task arguments."""

    args: Optional[str] = None
    cls: BaseTask = dataclasses.field(default=RunOperationTask, init=False)
    macro: Optional[str] = None
    which: str = dataclasses.field(default="run-operation", init=False)

    def __post_init__(self):
        """Support dictionary args by casting them to str after setting."""
        super().__post_init__()
        if isinstance(self.args, dict):
            self.args = str(self.args)


@dataclass
class SeedTaskConfig(TableMutabilityConfig):
    """Dbt seed task arguments."""

    cls: BaseTask = dataclasses.field(default=SeedTask, init=False)
    show: Optional[bool] = None
    compiled_target: Optional[Union[os.PathLike[str], str]] = None
    which: str = dataclasses.field(default="seed", init=False)


@dataclass
class SnapshotTaskConfig(SelectionConfig):
    """Dbt snapshot task arguments."""

    cls: BaseTask = dataclasses.field(default=SnapshotTask, init=False)
    compiled_target: Optional[Union[os.PathLike[str], str]] = None
    which: str = dataclasses.field(default="snapshot", init=False)


@dataclass
class SourceFreshnessTaskConfig(SelectionConfig):
    """Dbt source freshness task arguments."""

    cls: BaseTask = dataclasses.field(default=FreshnessTask, init=False)
    output: Optional[Union[os.PathLike, str, bytes]] = None
    which: str = dataclasses.field(default="source-freshness", init=False)


@dataclass
class TestTaskConfig(SelectionConfig):
    """Dbt test task arguments."""

    cls: BaseTask = dataclasses.field(default=TestTask, init=False)
    data: Optional[bool] = None
    indirect_selection: Optional[IndirectSelection] = None
    schema: Optional[bool] = None
    store_failures: Optional[bool] = None
    which: str = dataclasses.field(default="test", init=False)


class ConfigFactory(FromStrMixin, Enum):
    """Produce configurations for each dbt task."""

    BUILD = BuildTaskConfig
    COMPILE = CompileTaskConfig
    CLEAN = CleanTaskConfig
    DEBUG = DebugTaskConfig
    DEPS = DepsTaskConfig
    LIST = ListTaskConfig
    PARSE = ParseTaskConfig
    RUN = RunTaskConfig
    RUN_OPERATION = RunOperationTaskConfig
    SEED = SeedTaskConfig
    SNAPSHOT = SnapshotTaskConfig
    SOURCE = SourceFreshnessTaskConfig
    TEST = TestTaskConfig

    def create_config(self, *args, **kwargs) -> BaseConfig:
        """Instantiate a dbt task config with the given args and kwargs."""
        config = self.value(**kwargs)
        initialize_config_values(config)
        return config

    @property
    def fields(self) -> tuple[dataclasses.Field[Any], ...]:
        """Return the current configuration's fields."""
        return dataclasses.fields(self.value)


class DbtHook(BaseHook):
    """A hook to interact with dbt.

    Allows for running dbt tasks and provides required configurations for each task.
    """

    def get_config_factory(self, command: str) -> ConfigFactory:
        """Get a ConfigFactory given a dbt command string."""
        return ConfigFactory.from_str(command)

    def initialize_runtime_config(self, config: BaseConfig) -> RuntimeConfig:
        """Set environment flags and return a RuntimeConfig."""
        flags.reset()
        flags.set_from_args(config)
        return RuntimeConfig.from_args(config)

    def run_dbt_task(self, config: BaseConfig) -> tuple[bool, Optional[RunResult]]:
        """Run a dbt task with a given configuration and return the results.

        The configuration used determines the task that will be ran.

        Returns:
            A tuple containing a boolean indicating success and optionally the results
                of running the dbt command.
        """
        runtime_config = self.initialize_runtime_config(config)

        config.dbt_task.pre_init_hook(config)
        task = config.create_dbt_task()

        if not isinstance(task, DepsTask):
            # The deps command installs the dependencies, which means they may not exist
            # before deps runs and the following would raise a CompilationError.
            runtime_config.load_dependencies()

        with adapter_management():
            register_adapter(runtime_config)

            with track_run(task):
                results = task.run()
        success = task.interpret_results(results)

        return success, results