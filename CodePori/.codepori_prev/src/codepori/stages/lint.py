import logging
import shlex
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional

from src.codepori.context import Context
from src.codepori.stages.base import Stage, StageResult
from src.codepori.util.process_runner import ProcessRunner, ProcessRunResult

logger = logging.getLogger(__name__)


class LintStage(Stage):
    """
    A pipeline stage for static code analysis using a configurable linter.

    This stage is designed to run a linter tool (like ruff, flake8, mypy, etc.)
    on the codebase located in the project's root directory. It is a crucial
    step for ensuring code quality and adherence to coding standards before
    further stages like testing or deployment.

    The stage's execution is driven by configuration provided at initialization.
    It uses the `ProcessRunner` utility to execute the linter as a separate
    process, which ensures isolation and accurate capture of output and exit
    codes.

    The results of the linting process, including stdout, stderr, and the
    success status, are encapsulated in a `StageResult` object and appended to
    the `Context`. This allows subsequent stages or the final reporting
    mechanism to access and act upon the linting outcome.

    Attributes:
        STAGE_NAME (str): The unique identifier for this stage, set to "lint".
        DEFAULT_COMMAND (str): The default linter command to use if none is
                               provided in the configuration.
        DEFAULT_FAIL_ON_ERROR (bool): The default behavior regarding pipeline
                                      failure on linting errors.
        DEFAULT_TIMEOUT_SECONDS (int): The default timeout for the linter process.

    Configuration Example (in a project's main YAML/JSON config):
    ```yaml
    stages:
      - name: "lint"
        config:
          # The command to execute. Can include arguments.
          command: "ruff check . --fix --exit-non-zero-on-fix"
          # If true, the pipeline will halt if this stage fails.
          fail_on_error: true
          # Timeout in seconds for the linter process.
          timeout: 120
    ```
    """

    STAGE_NAME = "lint"
    DEFAULT_COMMAND = "ruff check ."
    DEFAULT_FAIL_ON_ERROR = True
    DEFAULT_TIMEOUT_SECONDS = 180  # Default timeout of 3 minutes

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        Initializes the LintStage with its specific configuration.

        This constructor sets up the stage by parsing its configuration,
        determining the linter command to run, and preparing the process
        runner utility.

        Args:
            config (Optional[Dict[str, Any]]): A dictionary containing the
                configuration for this stage. If None, default values will be
                used. Expected keys include 'command', 'fail_on_error', and
                'timeout'.

        Raises:
            TypeError: If a configuration value is of an incorrect type.
        """
        super().__init__(config or {})
        logger.info("Initializing LintStage...")
        self.command: List[str]
        self.fail_on_error: bool
        self.timeout: int
        self._parse_and_validate_config()
        self.process_runner = ProcessRunner()
        logger.info(f"LintStage initialized successfully. Command: '{' '.join(self.command)}'")

    def _parse_and_validate_config(self) -> None:
        """
        Parses and validates the configuration provided during initialization.

        This method extracts settings like the linter command and failure
        policy from the configuration dictionary. It provides default values
        for missing keys and performs type validation on provided values to
        prevent runtime errors.

        Raises:
            TypeError: If 'command' is not a string, 'fail_on_error' is not a
                       boolean, or 'timeout' is not a positive integer.
        """
        logger.debug(f"Parsing lint stage configuration: {self.config}")

        # Parse and validate the 'command'
        raw_command = self.config.get("command", self.DEFAULT_COMMAND)
        if not isinstance(raw_command, str):
            raise TypeError(
                f"Configuration error: 'command' must be a string, but got "
                f"{type(raw_command).__name__}."
            )
        self.command = shlex.split(raw_command)
        logger.debug(f"Linter command parsed to: {self.command}")

        # Parse and validate 'fail_on_error'
        self.fail_on_error = self.config.get("fail_on_error", self.DEFAULT_FAIL_ON_ERROR)
        if not isinstance(self.fail_on_error, bool):
            raise TypeError(
                f"Configuration error: 'fail_on_error' must be a boolean, but got "
                f"{type(self.fail_on_error).__name__}."
            )
        logger.debug(f"Policy 'fail_on_error' set to: {self.fail_on_error}")

        # Parse and validate 'timeout'
        self.timeout = self.config.get("timeout", self.DEFAULT_TIMEOUT_SECONDS)
        if not isinstance(self.timeout, int) or self.timeout <= 0:
            raise TypeError(
                f"Configuration error: 'timeout' must be a positive integer, but got "
                f"{self.timeout} of type {type(self.timeout).__name__}."
            )
        logger.debug(f"Process timeout set to: {self.timeout} seconds")

    def run(self, context: Context) -> Context:
        """
        Executes the configured linting command on the project's source code.

        This is the main entry point for the stage's logic. It retrieves the
        project's root directory from the context, executes the linter command
        within that directory, and handles the outcome. It captures standard
        output, standard error, and the process exit code, then formats this
        information into a `StageResult` which is added back into the context.

        Args:
            context (Context): The current execution context, which must contain
                               a 'project_root' pointing to the code to be
                               linted.

        Returns:
            Context: The context object, updated with the `StageResult` from
                     this linting run.

        Raises:
            FileNotFoundError: If the project root directory specified in the
                               context does not exist or is not a directory, or if
                               the linter executable cannot be found.
            subprocess.TimeoutExpired: If the linter process exceeds the
                                       configured timeout.
        """
        logger.info(f"--- Running Stage: {self.STAGE_NAME} ---")

        project_root = context.get("project_root")
        if not project_root or not Path(project_root).is_dir():
            error_message = f"Project root '{project_root}' not found or is not a directory."
            logger.error(error_message)
            stage_result = self._create_failure_result(
                message=error_message,
                details={"project_root": str(project_root)},
                error_type="ConfigurationError"
            )
            context.add_stage_result(stage_result)
            raise FileNotFoundError(error_message)

        target_dir = Path(project_root)
        logger.info(f"Executing linter command in directory: {target_dir.resolve()}")
        logger.info(f"Command: '{' '.join(self.command)}'")

        try:
            process_result = self.process_runner.run(
                command=self.command,
                cwd=target_dir,
                timeout=self.timeout
            )
            logger.debug(f"Linter process completed with exit code: {process_result.return_code}")
            stage_result = self._create_stage_result(process_result)

        except FileNotFoundError:
            error_message = (
                f"Linter command '{self.command[0]}' could not be executed. "
                "Please ensure the linter is installed and accessible in the system's PATH."
            )
            logger.error(error_message, exc_info=True)
            stage_result = self._create_failure_result(
                message=error_message,
                details={"command": self.command},
                error_type="ExecutableNotFoundError"
            )
            context.add_stage_result(stage_result)
            raise

        except subprocess.TimeoutExpired as e:
            error_message = (
                f"Linting process timed out after {self.timeout} seconds. "
                "The codebase may be too large or the linter may be stuck."
            )
            logger.error(error_message, exc_info=True)
            stage_result = self._create_failure_result(
                message=error_message,
                details={
                    "command": self.command,
                    "timeout_seconds": self.timeout,
                    "stdout_so_far": e.stdout.decode('utf-8', errors='ignore') if e.stdout else "",
                    "stderr_so_far": e.stderr.decode('utf-8', errors='ignore') if e.stderr else "",
                },
                error_type="TimeoutError"
            )
            context.add_stage_result(stage_result)
            raise

        except Exception as e:
            error_message = f"An unexpected error occurred during the linting stage: {e}"
            logger.critical(error_message, exc_info=True)
            stage_result = self._create_failure_result(
                message=error_message,
                details={"command": self.command, "exception": str(e)},
                error_type="UnexpectedError"
            )
            context.add_stage_result(stage_result)
            raise

        context.add_stage_result(stage_result)
        if not stage_result.success:
            logger.warning("Linting stage finished with errors.")
            if self.fail_on_error:
                logger.error("Pipeline will be halted because 'fail_on_error' is true.")
        else:
            logger.info("Linting stage finished successfully.")

        logger.info(f"--- Stage {self.STAGE_NAME} completed ---")
        return context

    def _create_stage_result(self, process_result: ProcessRunResult) -> StageResult:
        """
        Constructs a StageResult object from the linter's process execution result.

        Args:
            process_result (ProcessRunResult): The result object from the
                `ProcessRunner`, containing the exit code, stdout, and stderr.

        Returns:
            StageResult: A structured result object that represents the outcome
                         of the linting process.
        """
        is_success = process_result.return_code == 0

        message = (
            "Linting completed successfully. No issues found."
            if is_success
            else "Linting failed. Issues were found in the code."
        )

        details = {
            "command_executed": self.command,
            "return_code": process_result.return_code,
            "stdout": process_result.stdout,
            "stderr": process_result.stderr,
        }

        return StageResult(
            stage_name=self.STAGE_NAME,
            success=is_success,
            message=message,
            details=details,
        )

    def _create_failure_result(
        self, message: str, details: Dict[str, Any], error_type: str
    ) -> StageResult:
        """
        Creates a standardized StageResult for a failed stage execution.

        This helper method is used for creating consistent failure results,
        whether the failure is due to a configuration issue, a missing
        executable, or an unexpected exception.

        Args:
            message (str): The primary, user-friendly error message.
            details (Dict[str, Any]): A dictionary containing technical
                details about the failure.
            error_type (str): A string categorizing the type of error.

        Returns:
            StageResult: A StageResult object representing the failure.
        """
        failure_details = {"error_type": error_type, **details}
        return StageResult(
            stage_name=self.STAGE_NAME,
            success=False,
            message=message,
            details=failure_details,
        )

    def __repr__(self) -> str:
        """
        Provides a developer-friendly string representation of the LintStage.

        Returns:
            str: A string showing the class name and its configured command.
        """
        return f"LintStage(command=\"{' '.join(self.command)}\", fail_on_error={self.fail_on_error})"
