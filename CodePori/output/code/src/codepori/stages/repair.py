import logging
from typing import Optional, Dict, Any

from src.codepori.context import CodePoriContext
from src.codepori.llm.providers.base import LLMProvider
from src.codepori.stages.base import BaseStage
from src.codepori.utils.code_extractor import CodeExtractor

# Configure a logger for this module
logger = logging.getLogger(__name__)


class RepairExecutionError(Exception):
    """Custom exception for errors encountered during the repair stage."""
    pass


class MaxRepairAttemptsExceededError(RepairExecutionError):
    """Exception raised when the maximum number of repair attempts is exceeded."""
    def __init__(self, message="Maximum repair attempts exceeded."):
        self.message = message
        super().__init__(self.message)


class RepairStage(BaseStage):
    """Implements the code repair stage of the CodePori pipeline.

    This stage is activated when preceding stages, such as linting or testing,
    report failures. It leverages an LLM to automatically fix the identified
    issues in the generated code. It constructs a detailed prompt containing the
    original request, the faulty code, and the specific error feedback, then
    instructs the LLM to provide a corrected version.

    Attributes:
        max_repair_attempts (int): The maximum number of times this stage will
            attempt to repair the code before failing.
        llm_provider (LLMProvider): An instance of an LLM provider for making API calls.
    """

    DEFAULT_MAX_REPAIR_ATTEMPTS = 3

    REPAIR_PROMPT_TEMPLATE = """
    You are an expert Python developer tasked with fixing code that has failed validation.
    The original request was:
    ---
    {user_request}
    ---

    The following Python code was generated to fulfill the request, but it failed.
    ---
    File: {file_path}

    ```python
    {original_code}
    ```
    ---

    The code failed with the following errors. Please fix the code to resolve these issues.

    {feedback_section}

    Your task is to correct the code based on the feedback provided. 
    Analyze the errors carefully and rewrite the entire code block with the necessary fixes.
    Ensure the corrected code is complete, correct, and directly addresses all the reported issues.

    IMPORTANT: Respond ONLY with the complete, corrected Python code for the file `{file_path}` inside a single python markdown block. Do not include any explanations, apologies, or conversational text before or after the code.
    """

    def __init__(self, llm_provider: LLMProvider, config: Optional[Dict[str, Any]] = None):
        """Initializes the RepairStage.

        Args:
            llm_provider (LLMProvider): The language model provider to be used for
                generating code repairs.
            config (Optional[Dict[str, Any]]): A dictionary for configuration.
                Can contain 'max_repair_attempts'.
        """
        super().__init__(config)
        if not isinstance(llm_provider, LLMProvider):
            raise TypeError("llm_provider must be an instance of LLMProvider")
        self.llm_provider = llm_provider
        self.max_repair_attempts = self.config.get(
            'max_repair_attempts',
            self.DEFAULT_MAX_REPAIR_ATTEMPTS
        )
        logger.info(f"RepairStage initialized with max_repair_attempts={self.max_repair_attempts}")

    def execute(self, context: CodePoriContext) -> CodePoriContext:
        """Executes the code repair process.

        This method orchestrates the repair process by checking for feedback,
        managing repair attempt limits, constructing a prompt for the LLM,
        invoking the LLM, and updating the context with the repaired code.

        Args:
            context (CodePoriContext): The data context containing the code to be
                repaired, feedback, and other relevant information.

        Returns:
            CodePoriContext: The updated context, potentially with repaired code.

        Raises:
            MaxRepairAttemptsExceededError: If the number of repair attempts exceeds
                the configured maximum.
            RepairExecutionError: If no feedback is available to guide the repair or
                if the LLM fails to return a valid code block.
        """
        logger.info("--- Entering Repair Stage ---")
        context.repair_attempts += 1
        logger.info(f"Starting repair attempt {context.repair_attempts} of {self.max_repair_attempts}.")

        if context.repair_attempts > self.max_repair_attempts:
            raise MaxRepairAttemptsExceededError(
                f"Exceeded maximum of {self.max_repair_attempts} repair attempts."
            )

        lint_feedback = context.get_result('lint_feedback')
        test_feedback = context.get_result('test_feedback')

        if not lint_feedback and not test_feedback:
            logger.warning("RepairStage was called, but no lint or test feedback was found in the context. Skipping.")
            return context

        self._log_feedback_summary(lint_feedback, test_feedback)

        try:
            prompt = self._construct_repair_prompt(context, lint_feedback, test_feedback)
            logger.debug(f"Constructed repair prompt for LLM:\n{prompt}")

            llm_response = self.llm_provider.generate(prompt)

            repaired_code = self._extract_repaired_code(llm_response)

            if not repaired_code:
                raise RepairExecutionError("LLM failed to return a valid code block for repair.")

            logger.info("Successfully received and extracted repaired code from LLM.")
            # Update the context with the new code
            context.update_code(repaired_code)
            # Reset feedback so subsequent stages run on a clean slate
            context.add_result('lint_feedback', None)
            context.add_result('test_feedback', None)
            context.set_last_stage_status('repair', 'success')

        except Exception as e:
            logger.error(f"An error occurred during the repair stage: {e}", exc_info=True)
            context.set_last_stage_status('repair', 'failed', str(e))
            # Re-raise as a specific exception to be handled by the pipeline orchestrator
            if not isinstance(e, (RepairExecutionError, MaxRepairAttemptsExceededError)):
                raise RepairExecutionError(f"Failed during repair process: {e}") from e
            raise e

        logger.info("--- Exiting Repair Stage ---")
        return context

    def _construct_repair_prompt(
        self, 
        context: CodePoriContext, 
        lint_feedback: Optional[str], 
        test_feedback: Optional[str]
    ) -> str:
        """Constructs the full prompt to be sent to the LLM for code repair.

        Args:
            context (CodePoriContext): The current execution context.
            lint_feedback (Optional[str]): The feedback from the linting stage.
            test_feedback (Optional[str]): The feedback from the testing stage.

        Returns:
            str: The fully formatted prompt string.
        """
        feedback_parts = []
        if lint_feedback:
            feedback_parts.append(f"LINTING ERRORS:\n```\n{lint_feedback.strip()}\n```")

        if test_feedback:
            feedback_parts.append(f"TESTING FAILURES:\n```\n{test_feedback.strip()}\n```")

        feedback_section = "\n\n".join(feedback_parts)

        return self.REPAIR_PROMPT_TEMPLATE.format(
            user_request=context.initial_request,
            file_path=context.target_file_path,
            original_code=context.get_code(),
            feedback_section=feedback_section,
        )

    def _extract_repaired_code(self, llm_response: str) -> Optional[str]:
        """Extracts the Python code block from the LLM's response.

        Args:
            llm_response (str): The raw response string from the LLM.

        Returns:
            Optional[str]: The extracted code as a string, or None if no code block
                is found.
        """
        logger.debug("Attempting to extract repaired code from LLM response.")
        try:
            code = CodeExtractor.extract_python_code(llm_response)
            if code:
                logger.info("Successfully extracted python code block.")
                return code
            else:
                logger.warning("Could not find a Python code block in the LLM's repair response.")
                return None
        except Exception as e:
            logger.error(f"An error occurred during code extraction: {e}", exc_info=True)
            return None

    def _log_feedback_summary(self, lint_feedback: Optional[str], test_feedback: Optional[str]):
        """Logs a summary of the feedback that needs to be addressed.

        Args:
            lint_feedback (Optional[str]): The feedback from the linting stage.
            test_feedback (Optional[str]): The feedback from the testing stage.
        """
        if lint_feedback:
            logger.info("Found linting feedback to be addressed:")
            for line in lint_feedback.strip().split('\n'):
                logger.info(f"  [LINT] {line}")

        if test_feedback:
            logger.info("Found testing feedback to be addressed:")
            for line in test_feedback.strip().split('\n'):
                logger.info(f"  [TEST] {line}")
