#
# Copyright (c) 2023, The CodePori Project. All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
#

"""
Defines the core components for building pipeline stages.

This module provides the abstract base class `PipelineStage` that all concrete
pipeline stages must inherit from. It also includes a `PipelineContext` for
managing shared state between stages and custom exception classes for robust
error handling within the pipeline.
"""

import abc
import logging
import time
from typing import Any, Dict, Generic, Optional, TypeVar

# A generic type for values stored in the context
V = TypeVar('V')


class PipelineError(Exception):
    """Base exception for all errors originating from the pipeline."""
    pass


class StageExecutionError(PipelineError):
    """
    Exception raised when an error occurs during the execution of a pipeline stage.

    Attributes:
        stage_name (str): The name of the stage that failed.
        original_exception (Exception): The original exception that was caught.
    """
    def __init__(self, stage_name: str, original_exception: Exception):
        self.stage_name = stage_name
        self.original_exception = original_exception
        message = f"Error during execution of stage '{stage_name}': {original_exception}"
        super().__init__(message)


class PipelineContext:
    """
    A data carrier object for sharing state between pipeline stages.

    This class acts as a sophisticated dictionary, providing a structured way to
    pass data, metadata, and configuration through the pipeline. It ensures that
    data is accessed in a consistent manner and can be extended to include
    features like immutability for certain keys or type validation.
    """

    def __init__(self, initial_data: Optional[Dict[str, Any]] = None):
        """
        Initializes the PipelineContext.

        Args:
            initial_data (Optional[Dict[str, Any]]): An optional dictionary to
                populate the initial context state. Defaults to None.
        """
        self._data: Dict[str, Any] = initial_data.copy() if initial_data else {}
        self._logger = logging.getLogger(self.__class__.__name__)
        self._logger.info("PipelineContext initialized.")

    def set(self, key: str, value: Any) -> None:
        """
        Sets a value in the context.

        Args:
            key (str): The key to associate with the value.
            value (Any): The value to store.
        """
        self._logger.debug("Setting context key '%s'.", key)
        self._data[key] = value

    def get(self, key: str, default: Optional[V] = None) -> Any | V:
        """
        Retrieves a value from the context.

        Args:
            key (str): The key of the value to retrieve.
            default (Optional[V]): The default value to return if the key is not
                found. Defaults to None.

        Returns:
            Any | V: The value associated with the key, or the default value.
        """
        value = self._data.get(key, default)
        self._logger.debug("Getting context key '%s'. Found: %s.", key, value is not default)
        return value

    def has(self, key: str) -> bool:
        """
        Checks if a key exists in the context.

        Args:
            key (str): The key to check.

        Returns:
            bool: True if the key exists, False otherwise.
        """
        return key in self._data

    def remove(self, key: str) -> bool:
        """
        Removes a key-value pair from the context if it exists.

        Args:
            key (str): The key to remove.

        Returns:
            bool: True if the key was found and removed, False otherwise.
        """
        if self.has(key):
            self._logger.debug("Removing context key '%s'.", key)
            del self._data[key]
            return True
        self._logger.warning("Attempted to remove non-existent key '%s'.", key)
        return False

    def get_all(self) -> Dict[str, Any]:
        """
        Returns a copy of the entire data dictionary.

        This is useful for inspection and debugging purposes. Modifying the
        returned dictionary will not affect the context's internal state.

        Returns:
            Dict[str, Any]: A copy of all data stored in the context.
        """
        return self._data.copy()

    def __repr__(self) -> str:
        """
        Provides a string representation of the context.

        Returns:
            str: A string representation showing the class name and stored keys.
        """
        keys = ", ".join(self._data.keys())
        return f"<{self.__class__.__name__} keys=[{keys}]>"


class PipelineStage(abc.ABC):
    """
    Abstract Base Class for a single stage in a data processing pipeline.

    This class defines the standard interface for all pipeline stages. Each
    concrete stage must implement the `execute` method, which contains the
    core logic of that stage.

    The `PipelineStage` provides a `__call__` method that acts as a template,
    wrapping the `execute` method with common functionality such as logging,
    timing, and error handling. This ensures consistency and reduces boilerplate
    code in concrete stage implementations.

    Attributes:
        logger (logging.Logger): A logger instance for the stage.
    """

    def __init__(self, **kwargs: Any):
        """
        Initializes the pipeline stage.

        The constructor sets up a dedicated logger for the subclass instance.
        It can be extended by subclasses to accept configuration parameters.

        Args:
            **kwargs (Any): Arbitrary keyword arguments that can be used for
                stage-specific configuration.
        """
        self.logger = logging.getLogger(self.name)
        self.config = kwargs
        self.logger.info("Stage '%s' initialized.", self.name)

    @property
    def name(self) -> str:
        """
        The name of the pipeline stage.

        By default, it is the name of the class. This can be overridden in
        subclasses if a different name is desired.

        Returns:
            str: The name of the stage.
        """
        return self.__class__.__name__

    @abc.abstractmethod
    def execute(self, context: PipelineContext) -> PipelineContext:
        """
        The core logic of the pipeline stage.

        This method must be implemented by all concrete subclasses. It receives
        the pipeline context, performs its specific processing tasks, and can
        modify the context by adding, updating, or removing data.

        Args:
            context (PipelineContext): The shared context object containing data
                from previous stages.

        Returns:
            PipelineContext: The modified context object to be passed to the next
                stage.

        Raises:
            NotImplementedError: If the subclass does not implement this method.
        """
        raise NotImplementedError("Subclasses must implement the 'execute' method.")

    def __call__(self, context: PipelineContext) -> PipelineContext:
        """
        Executes the stage and handles common tasks like logging and timing.

        This method should not be overridden by subclasses. Instead, the core
        logic should be placed in the `execute` method. This method acts as
        a wrapper (Template Method Design Pattern).

        Args:
            context (PipelineContext): The pipeline context to be processed.

        Returns:
            PipelineContext: The context after processing by the stage.

        Raises:
            StageExecutionError: If any exception occurs during the execution of
                the `execute` method.
        """
        self.logger.info("--- Starting stage: %s ---", self.name)
        start_time = time.perf_counter()

        try:
            # The primary work is delegated to the abstract method.
            updated_context = self.execute(context)
            if not isinstance(updated_context, PipelineContext):
                raise TypeError(
                    f"Stage '{self.name}' must return a PipelineContext instance, "
                    f"but returned type '{type(updated_context).__name__}'"
                )
        except Exception as e:
            self.logger.error(
                "Stage '%s' failed due to an exception: %s",
                self.name, e, exc_info=True
            )
            # Wrap the original exception in a custom, more informative exception.
            raise StageExecutionError(stage_name=self.name, original_exception=e) from e
        finally:
            end_time = time.perf_counter()
            duration = end_time - start_time
            self.logger.info(
                "--- Finished stage: %s in %.4f seconds ---",
                self.name, duration
            )

        return updated_context
