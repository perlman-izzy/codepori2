import logging
import json
from pathlib import Path
from typing import Any, Dict, Optional

# Attempt to import black, but don't make it a hard dependency
try:
    import black
    BLACK_AVAILABLE = True
except ImportError:
    BLACK_AVAILABLE = False

from src.codepori.stages.base import PipelineStage, StageData
from src.codepori.llms.state import State

logger = logging.getLogger(__name__)

class FinalizationError(Exception):
    """Custom exception for errors during the finalization stage."""
    pass


class FinalizeStage(PipelineStage):
    """Implements the final stage of the code generation pipeline.

    This stage is responsible for performing concluding tasks such as code
    formatting, generating a summary report, and any other necessary cleanup.
    It is designed to be configurable to enable or disable specific tasks.

    Configuration options:
        do_formatting (bool): If True, formats the generated code using `black`.
                              Defaults to True.
        formatter_config (dict): Configuration for the `black` formatter.
                                 See `black.FileMode` for options.
                                 Defaults to `{"line_length": 88}`.
        do_report (bool): If True, generates a summary report of the process.
                           Defaults to True.
        report_format (str): The format for the report ('json' or 'text').
                             Defaults to 'text'.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initializes the FinalizeStage.

        Args:
            config (Optional[Dict[str, Any]]): A dictionary containing configuration
                for the stage. See class docstring for details.
        """
        super().__init__(config)
        self._config = config if config is not None else {}

        # Set default values for configuration options
        self.do_formatting = self._config.get("do_formatting", True)
        self.formatter_config = self._config.get("formatter_config", {"line_length": 88})
        self.do_report = self._config.get("do_report", True)
        self.report_format = self._config.get("report_format", "text")

        if self.do_formatting and not BLACK_AVAILABLE:
            logger.warning(
                "'black' library not found, but formatting is enabled. "
                "Code formatting will be skipped. Please install it with 'pip install black'."
            )
            self.do_formatting = False

    def run(self, state: State) -> StageData:
        """Executes the finalization tasks on the given state.

        Args:
            state (State): The current state of the pipeline, containing the
                         generated code and other metadata.

        Returns:
            StageData: An object containing the output data of this stage,
                       including the finalized code and report.

        Raises:
            FinalizationError: If a critical error occurs during finalization.
        """
        logger.info("Entering finalization stage...")

        final_code = state.get("final_code")
        if not final_code or not isinstance(final_code, str):
            raise FinalizationError("No final code found in the state to process.")

        # --- Code Formatting --- #
        if self.do_formatting:
            try:
                logger.info("Formatting final code...")
                final_code = self._format_code(final_code)
                logger.info("Code formatting completed successfully.")
            except Exception as e:
                logger.error(f"Failed to format code: {e}", exc_info=True)
                # We don't raise an exception here; formatting is a non-critical enhancement.

        # --- Report Generation --- #
        report = None
        if self.do_report:
            try:
                logger.info("Generating summary report...")
                report_content = self._create_report_content(state)
                report = self._format_report(report_content)
                logger.info("Summary report generated successfully.")
            except Exception as e:
                error_msg = f"Failed to generate report: {e}"
                logger.error(error_msg, exc_info=True)
                # Report generation is also non-critical.
                report = f"Error: {error_msg}"

        logger.info("Finalization stage completed.")

        # Update state with the processed artifacts
        state.set("final_code", final_code)
        state.set("summary_report", report)

        return StageData(is_success=True, data={"final_code": final_code, "report": report})

    def _format_code(self, code: str) -> str:
        """Formats the given Python code string using the `black` library.

        Args:
            code (str): The Python code to format.

        Returns:
            str: The formatted code.

        Raises:
            black.NothingChanged: If the code was already formatted.
            Exception: For other `black` formatting errors.
        """
        if not BLACK_AVAILABLE:
            logger.warning("Skipping formatting because 'black' is not installed.")
            return code

        try:
            # Create a FileMode object from the configuration dictionary
            file_mode = black.Mode(**self.formatter_config)
            formatted_code = black.format_str(code, mode=file_mode)
            return formatted_code
        except black.NothingChanged:
            logger.info("Code is already well-formatted. No changes made.")
            return code
        except Exception as e:
            # Re-raise to be handled by the run method
            raise FinalizationError(f"Black formatter failed: {e}") from e

    def _create_report_content(self, state: State) -> Dict[str, Any]:
        """Creates the structured content for the summary report.

        Args:
            state (State): The current pipeline state.

        Returns:
            Dict[str, Any]: A dictionary containing key metrics and information.
        """
        conversation_history = state.get("conversation_history", [])

        # Calculate some basic metrics
        num_iterations = 0
        if conversation_history:
            # Assuming conversation is a list of tuples (role, content)
            # and each dev's response is an iteration.
            num_iterations = sum(1 for item in conversation_history if item.get('role') == 'dev_1')

        initial_prompt = state.get("initial_prompt", "Not available")
        final_code = state.get("final_code", "")
        target_path = state.get("target_path", "Not specified")

        report_data = {
            "summary": {
                "status": "Success",
                "target_file": str(target_path),
                "total_iterations": num_iterations,
                "final_code_lines": len(final_code.splitlines()),
            },
            "details": {
                "initial_prompt": initial_prompt,
                "formatting_applied": self.do_formatting,
                "formatter_config": self.formatter_config if self.do_formatting else "N/A",
            },
            # Intentionally not including full code in the report for brevity,
            # but it could be added if needed.
        }
        return report_data

    def _format_report(self, content: Dict[str, Any]) -> str:
        """Formats the report content into the desired string format.

        Args:
            content (Dict[str, Any]): The structured report data.

        Returns:
            str: The formatted report string.
        """
        if self.report_format == "json":
            return json.dumps(content, indent=2)
        elif self.report_format == "text":
            summary = content.get("summary", {})
            details = content.get("details", {})

            report_lines = [
                "=============================================",
                "      Code Generation Process Summary      ",
                "=============================================",
                f"Status: {summary.get('status', 'N/A')}",
                f"Target File: {summary.get('target_file', 'N/A')}",
                f"Total Iterations: {summary.get('total_iterations', 'N/A')}",
                f"Final Code Lines: {summary.get('final_code_lines', 'N/A')}",
                "---------------------------------------------",
                "Configuration Details:",
                f"  Formatting Applied: {details.get('formatting_applied', 'N/A')}",
                f"  Formatter Config: {json.dumps(details.get('formatter_config', {}))}",
                "---------------------------------------------",
                "Initial Prompt Snippet:",
                f"  {(details.get('initial_prompt', '')[:100] + '...') if details.get('initial_prompt') else 'N/A'}",
                "=============================================",
            ]
            return "\n".join(report_lines)
        else:
            logger.warning(f"Unknown report format '{self.report_format}'. Defaulting to JSON.")
            return json.dumps(content, indent=2)
