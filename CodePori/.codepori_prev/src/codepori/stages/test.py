import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from src.codepori.context.context import Context
from src.codepori.stages.base import Stage
from src.codepori.util.process_runner import ProcessResult, ProcessRunner

logger = logging.getLogger(__name__)


class TestStage(Stage):
    """Implements the testing stage of the code generation pipeline.

    This stage is responsible for executing a project's test suite to verify the
    correctness of the generated code. It utilizes a configurable process runner
    to invoke a test framework like pytest. The stage captures detailed test
    results, including stdout, stderr, exit code, and a summary of passed and
    failed tests. These results are then stored in the shared context, making
    them available for subsequent stages, particularly the RepairStage, which
    may use this information to identify and fix issues in the code.

    Attributes:
        test_command (List[str]): The command and arguments to execute the tests.
        timeout (int): The maximum time in seconds allowed for the test run.
        env (Optional[Dict[str, str]]): Environment variables for the test process.
        process_runner (ProcessRunner): An instance of the process runner utility.
    """

    def __init__(
        self,
        test_command: Optional[List[str]] = None,
        timeout: int = 300,
        env: Optional[Dict[str, str]] = None,
    ):
        """Initializes the TestStage with configuration for the test execution.

        Args:
            test_command (Optional[List[str]], optional): The command to run the
                tests. Defaults to ["pytest", "-v"].
            timeout (int, optional): The timeout in seconds for the test
                command. Defaults to 300.
            env (Optional[Dict[str, str]], optional): A dictionary of environment
                variables to set for the test process. Defaults to None.
        """
        self._name = "test"
        self._description = "Executes the test suite for the generated project."
        self.test_command = test_command or ["pytest", "-v"]
        self.timeout = timeout
        self.env = env
        self.process_runner = ProcessRunner()

    @property
    def name(self) -> str:
        """Gets the name of the stage."""
        return self._name

    @property
    def description(self) -> str:
        """Gets the description of the stage."""
        return self._description

    def execute(self, context: Context) -> Context:
        """Executes the testing logic for the stage.

        This method orchestrates the test execution process by:
        1. Validating the context to ensure a project directory is available.
        2. Running the configured test command within that directory.
        3. Parsing the results from the process output.
        4. Storing the comprehensive test results back into the context.

        Args:
            context (Context): The shared context object containing project data.

        Returns:
            Context: The updated context object with detailed test results.
        """
        logger.info(f"--- Starting {self.name.upper()} Stage ---")

        try:
            project_dir = self._validate_context(context)
            process_result = self._run_tests(project_dir)
            test_results = self._parse_and_package_results(process_result)
        except (ValueError, FileNotFoundError) as e:
            logger.error(f"Pre-flight check failed for Test Stage: {e}")
            test_results = self._create_error_result(str(e))
        except Exception as e:
            logger.error(
                f"An unexpected error occurred during test execution: {e}", exc_info=True
            )
            test_results = self._create_error_result(
                f"An unexpected error occurred: {str(e)}"
            )

        context.set("test_results", test_results)
        logger.info(
            f"Test results stored in context. Passed: {test_results.get('passed')}"
        )
        logger.info(f"--- Finished {self.name.upper()} Stage ---")
        return context

    def _validate_context(self, context: Context) -> str:
        """Validates that the necessary information exists in the context.

        Args:
            context (Context): The shared context object.

        Returns:
            str: The validated project directory path.

        Raises:
            ValueError: If 'project_dir' is not set in the context.
            FileNotFoundError: If the project directory does not exist.
        """
        logger.debug("Validating context for test stage.")
        project_dir = context.get("project_dir")

        if not project_dir:
            raise ValueError("Project directory not found in context.")

        if not os.path.isdir(project_dir):
            raise FileNotFoundError(f"Project directory '{project_dir}' does not exist.")

        logger.debug(f"Context validated. Project directory: {project_dir}")
        return project_dir

    def _run_tests(self, project_dir: str) -> ProcessResult:
        """Runs the test command in the specified directory.

        Args:
            project_dir (str): The path to the project directory.

        Returns:
            ProcessResult: The result from the process runner.
        """
        logger.info(f"Running test command: `{' '.join(self.test_command)}`")
        logger.info(f"Working directory: {project_dir}")
        if self.env:
            logger.info(f"Using custom environment variables: {list(self.env.keys())}")

        try:
            result = self.process_runner.run(
                self.test_command, cwd=project_dir, timeout=self.timeout, env=self.env
            )
            logger.info(f"Test command finished with exit code: {result.exit_code}")
            return result
        except FileNotFoundError:
            msg = f"Test command '{self.test_command[0]}' not found. Make sure it is installed and in the system's PATH."
            logger.error(msg)
            return ProcessResult(exit_code=-1, stdout="", stderr=msg, timed_out=False)
        except Exception as e:
            msg = f"An unexpected exception occurred while running the test process: {e}"
            logger.error(msg, exc_info=True)
            return ProcessResult(exit_code=-1, stdout="", stderr=msg, timed_out=False)

    def _parse_and_package_results(
        self, process_result: ProcessResult
    ) -> Dict[str, Any]:
        """Parses the process result and packages it into a structured dictionary.

        This method analyzes the stdout/stderr from pytest to extract a summary
        of test outcomes.

        Args:
            process_result (ProcessResult): The raw result from the process runner.

        Returns:
            Dict[str, Any]: A dictionary containing detailed test results.
        """
        logger.debug("Parsing test execution results.")

        if process_result.stdout:
            logger.debug(f"Test stdout:\n---\n{process_result.stdout}\n---")
        if process_result.stderr:
            logger.debug(f"Test stderr:\n---\n{process_result.stderr}\n---")

        test_passed = process_result.exit_code == 0
        summary, passed_count, failed_count = self._parse_pytest_summary(
            process_result.stdout + process_result.stderr
        )

        return {
            "command": self.test_command,
            "exit_code": process_result.exit_code,
            "stdout": process_result.stdout,
            "stderr": process_result.stderr,
            "passed": test_passed,
            "timed_out": process_result.timed_out,
            "summary": summary,
            "passed_count": passed_count,
            "failed_count": failed_count,
        }

    def _parse_pytest_summary(self, output: str) -> Tuple[str, int, int]:
        """Parses pytest output to find the test summary line.

        This is a best-effort parser and may not work with all pytest versions
        or configurations. It looks for a line like "== X failed, Y passed in Zs ==".

        Args:
            output (str): The combined stdout and stderr from the test run.

        Returns:
            Tuple[str, int, int]: A tuple containing the summary string,
            number of passed tests, and number of failed tests.
        """
        summary_line = ""
        passed_count = 0
        failed_count = 0

        summary_pattern = re.compile(r"=+ (.*) in [\d\.]+s =+")
        match = summary_pattern.search(output)

        if match:
            summary_line = match.group(1).strip()
            logger.debug(f"Found pytest summary line: '{summary_line}'")

            failed_match = re.search(r"(\d+) failed", summary_line)
            if failed_match:
                failed_count = int(failed_match.group(1))

            passed_match = re.search(r"(\d+) passed", summary_line)
            if passed_match:
                passed_count = int(passed_match.group(1))
        else:
            summary_line = "Could not parse pytest summary."
            logger.warning(summary_line)

        return summary_line, passed_count, failed_count

    def _create_error_result(self, error_message: str) -> Dict[str, Any]:
        """Creates a standardized dictionary for test results in case of an error.

        Args:
            error_message (str): The error message to include.

        Returns:
            Dict[str, Any]: A structured dictionary representing the failure.
        """
        return {
            "command": self.test_command,
            "exit_code": -1,
            "stdout": "",
            "stderr": error_message,
            "passed": False,
            "timed_out": False,
            "summary": "Test execution failed due to a system error.",
            "passed_count": 0,
            "failed_count": 0,
        }
