import abc
import logging
from typing import Any, Dict, List, Set, Type

# Configure a logger for the pipeline module
logger = logging.getLogger(__name__)


class PipelineError(Exception):
    """Base exception for all pipeline-related errors."""
    pass


class PipelineConfigurationError(PipelineError):
    """Raised when the pipeline is configured incorrectly.

    This typically occurs during the validation phase before execution, for example,
    if a stage's required inputs are not provided by previous stages.
    """
    pass


class PipelineExecutionError(PipelineError):
    """Raised when an error occurs during the execution of a pipeline stage.

    Attributes:
        stage (PipelineStage): The stage that failed during execution.
        original_exception (Exception): The original exception that was caught.
    """

    def __init__(self, stage: 'PipelineStage', original_exception: Exception):
        self.stage = stage
        self.original_exception = original_exception
        message = f"Error in stage '{type(stage).__name__}': {original_exception}"
        super().__init__(message)


class PipelineContext:
    """A data carrier object that holds the state between pipeline stages.

    This class acts as a sophisticated dictionary, providing methods to safely
    access and modify the pipeline's state. It stores all data passed from one
    stage to the next.

    Attributes:
        _data (Dict[str, Any]): The internal dictionary holding the context data.
    """

    def __init__(self, initial_data: Dict[str, Any] = None):
        """Initializes the PipelineContext.

        Args:
            initial_data (Dict[str, Any], optional): A dictionary with initial
                data for the context. Defaults to None, creating an empty context.
        """
        self._data = initial_data or {}
        logger.debug("PipelineContext initialized with keys: %s", self._data.keys())

    def set(self, key: str, value: Any) -> None:
        """Sets a value in the context.

        Args:
            key (str): The key to identify the data.
            value (Any): The data to store.
        """
        logger.debug("Setting context key '%s'", key)
        self._data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        """Gets a value from the context.

        Args:
            key (str): The key of the data to retrieve.
            default (Any, optional): The default value to return if the key is not
                found. Defaults to None.

        Returns:
            Any: The value associated with the key, or the default value.
        """
        value = self._data.get(key, default)
        logger.debug("Getting context key '%s' (found: %s)", key, key in self._data)
        return value

    def has(self, key: str) -> bool:
        """Checks if a key exists in the context.

        Args:
            key (str): The key to check for.

        Returns:
            bool: True if the key exists, False otherwise.
        """
        return key in self._data

    def has_all(self, keys: Set[str]) -> bool:
        """Checks if all specified keys exist in the context.

        Args:
            keys (Set[str]): A set of keys to check for.

        Returns:
            bool: True if all keys exist, False otherwise.
        """
        return keys.issubset(self._data.keys())

    @property
    def available_keys(self) -> Set[str]:
        """Provides the set of all keys currently available in the context.

        Returns:
            Set[str]: A set of all keys in the context.
        """
        return set(self._data.keys())

    def __repr__(self) -> str:
        """Provides a string representation of the context.

        Returns:
            str: A string showing the class name and the keys it contains.
        """
        return f"<PipelineContext keys={list(self._data.keys())}>"


class PipelineStage(abc.ABC):
    """An abstract base class for a single stage in a processing pipeline.

    Each concrete stage must implement the `execute` method, which performs
    the stage's logic. It's also required to declare its input dependencies
    and the outputs it guarantees via the `required_inputs` and
    `provided_outputs` properties.
    """

    @property
    @abc.abstractmethod
    def required_inputs(self) -> Set[str]:
        """Specifies the set of context keys required for this stage to run.

        Returns:
            Set[str]: A set of string keys that must be present in the
                PipelineContext before `execute` is called.
        """
        pass

    @property
    @abc.abstractmethod
    def provided_outputs(self) -> Set[str]:
        """Specifies the set of context keys this stage will add or update.

        Returns:
            Set[str]: A set of string keys that this stage guarantees to have
                set in the PipelineContext after `execute` completes successfully.
        """
        pass

    @abc.abstractmethod
    def execute(self, context: PipelineContext) -> PipelineContext:
        """Executes the logic for this pipeline stage.

        This method takes the current pipeline context, performs its operations
        (which may involve reading from and writing to the context), and then
        returns the (potentially modified) context for the next stage.

        Args:
            context (PipelineContext): The current state of the pipeline.

        Returns:
            PipelineContext: The updated context after this stage's execution.

        Raises:
            Exception: Any exception raised during execution will be caught by the
                pipeline runner and wrapped in a PipelineExecutionError.
        """
        pass

    def __repr__(self) -> str:
        """Provides a developer-friendly representation of the stage.

        Returns:
            str: The class name of the stage.
        """
        return self.__class__.__name__


class Pipeline:
    """Orchestrates the execution of a sequence of pipeline stages.

    The Pipeline is responsible for validating the sequence of stages, running
    them in order, and managing the `PipelineContext` as it's passed between
    them. It provides robust error handling and logging.

    Attributes:
        stages (List[PipelineStage]): The list of stage objects to be executed.
    """

    def __init__(self, stages: List[PipelineStage]):
        """Initializes the Pipeline with a list of stages.

        Args:
            stages (List[PipelineStage]): An ordered list of PipelineStage
                instances to be executed.

        Raises:
            ValueError: If the stages list is empty or contains non-PipelineStage objects.
        """
        if not stages:
            raise ValueError("Pipeline must be initialized with at least one stage.")
        if not all(isinstance(s, PipelineStage) for s in stages):
            raise ValueError("All elements in the stages list must be instances of PipelineStage.")

        self.stages = stages
        logger.info("Pipeline initialized with %d stages: %s", len(stages), [str(s) for s in stages])

    def validate(self, initial_context_keys: Set[str]) -> None:
        """Validates the pipeline's configuration before execution.

        This method checks that the input and output dependencies between stages
        are met. It ensures that for each stage, its required inputs are available
        either from the initial context or from the outputs of previous stages.

        Args:
            initial_context_keys (Set[str]): The keys present in the context at the
                start of the pipeline run.

        Raises:
            PipelineConfigurationError: If a stage's required input is not provided
                by the initial context or any preceding stage.
        """
        logger.info("Validating pipeline configuration...")
        available_keys = set(initial_context_keys)

        for stage in self.stages:
            stage_name = type(stage).__name__
            required = stage.required_inputs
            missing_keys = required - available_keys

            if missing_keys:
                msg = (
                    f"Configuration error in stage '{stage_name}'. "
                    f"Missing required context keys: {sorted(list(missing_keys))}. "
                    f"Available keys before this stage: {sorted(list(available_keys))}."
                )
                logger.error(msg)
                raise PipelineConfigurationError(msg)

            # Add the outputs of the current stage to the set of available keys
            # for subsequent stages.
            available_keys.update(stage.provided_outputs)

        logger.info("Pipeline validation successful.")

    def run(self, context: PipelineContext) -> PipelineContext:
        """Executes all stages in the pipeline sequentially.

        Args:
            context (PipelineContext): The initial context for the pipeline run.

        Returns:
            PipelineContext: The final context after all stages have been executed.

        Raises:
            PipelineConfigurationError: If the pre-run validation fails.
            PipelineExecutionError: If any stage fails during its execution.
        """
        logger.info("Starting pipeline run...")

        # First, validate the entire pipeline configuration.
        self.validate(context.available_keys)

        current_context = context

        for stage in self.stages:
            stage_name = type(stage).__name__
            logger.info("--- Executing stage: %s ---", stage_name)
            try:
                current_context = stage.execute(current_context)
                if not isinstance(current_context, PipelineContext):
                    raise TypeError(
                        f"Stage '{stage_name}' did not return a PipelineContext object. "
                        f"Got '{type(current_context).__name__}' instead."
                    )
                logger.info("--- Stage '%s' completed successfully ---", stage_name)
            except Exception as e:
                logger.error("Pipeline execution failed at stage '%s'.", stage_name, exc_info=True)
                raise PipelineExecutionError(stage, e) from e

        logger.info("Pipeline run completed successfully.")
        return current_context
