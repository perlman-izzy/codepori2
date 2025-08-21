import logging
import os
from typing import Dict, Any, Optional

from src.codepori.llm.llm_client import LLMClient
from src.codepori.managers.context_manager import ContextManager
from src.codepori.managers.filesystem_manager import FileSystemManager
from src.codepori.stages.base_stage import Stage
from src.codepori.models.plan import Plan, FilePlan
from src.codepori.prompts.prompt_loader import PromptLoader

logger = logging.getLogger(__name__)


class GenerateStage(Stage):
    """Implements the code generation stage of the software development process.

    This stage iterates through the file plans generated in the planning stage,
    constructs prompts for each file, and uses an LLM to generate the code.
    The generated code is then saved to the specified output directory.

    Attributes:
        llm_client (LLMClient): The client for interacting with the language model.
        filesystem_manager (FileSystemManager): The manager for file system operations.
        prompt_loader (PromptLoader): The loader for retrieving prompt templates.
        output_directory (str): The root directory where generated code will be saved.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        filesystem_manager: FileSystemManager,
        prompt_loader: PromptLoader,
        output_directory: str = "./output/code",
    ):
        """Initializes the GenerateStage with necessary dependencies.

        Args:
            llm_client (LLMClient): An instance of the LLM client.
            filesystem_manager (FileSystemManager): An instance of the filesystem manager.
            prompt_loader (PromptLoader): An instance of the prompt loader.
            output_directory (str): The path to the output directory for generated code.

        Raises:
            TypeError: If any of the dependency arguments are not of the expected type.
        """
        if not isinstance(llm_client, LLMClient):
            raise TypeError("llm_client must be an instance of LLMClient")
        if not isinstance(filesystem_manager, FileSystemManager):
            raise TypeError("filesystem_manager must be an instance of FileSystemManager")
        if not isinstance(prompt_loader, PromptLoader):
            raise TypeError("prompt_loader must be an instance of PromptLoader")

        self.llm_client = llm_client
        self.filesystem_manager = filesystem_manager
        self.prompt_loader = prompt_loader
        self.output_directory = output_directory
        logger.info("GenerateStage initialized with output directory: %s", self.output_directory)

    def execute(self, context: ContextManager) -> None:
        """Executes the code generation logic for the entire project.

        Retrieves the project plan from the context, generates code for each file
        in the plan, and saves it to the output directory.

        Args:
            context (ContextManager): The context manager containing shared project data.

        Raises:
            KeyError: If 'plan' or 'project_description' is not found in the context.
            TypeError: If the 'plan' object in the context is not a valid Plan instance.
        """
        logger.info("Starting code generation stage.")
        try:
            plan: Optional[Plan] = context.get('plan')
            project_description: str = context.get('project_description')
        except KeyError as e:
            logger.error("Missing required key in context: %s. Aborting generation.", e)
            raise

        if not isinstance(plan, Plan):
            logger.error("Context 'plan' is not a valid Plan object. Found type: %s", type(plan))
            raise TypeError("Context 'plan' must be an instance of the Plan model.")

        if not plan.files:
            logger.warning("The project plan contains no files to generate. Skipping generation stage.")
            return

        logger.info("Ensuring output directory exists: %s", self.output_directory)
        self.filesystem_manager.create_directory(self.output_directory)

        accumulated_code: Dict[str, str] = {}

        total_files = len(plan.files)
        for i, file_plan in enumerate(plan.files, 1):
            logger.info("--- Generating file %d of %d: %s ---", i, total_files, file_plan.path)
            self._generate_and_save_file(file_plan, project_description, accumulated_code)

        context.set('generated_code', accumulated_code)
        logger.info("Code generation stage completed successfully.")

    def _generate_and_save_file(
        self,
        file_plan: FilePlan,
        project_description: str,
        accumulated_code: Dict[str, str],
    ) -> None:
        """Generates and saves a single file, updating the accumulated code.

        Args:
            file_plan (FilePlan): The plan for the file to be generated.
            project_description (str): A high-level description of the project.
            accumulated_code (Dict[str, str]): A dictionary of already generated files
                                               and their content to provide context. This
                                               dictionary is updated with the new file's code.
        """
        try:
            prompt = self._construct_generation_prompt(
                project_description,
                file_plan.path,
                file_plan.purpose,
                accumulated_code
            )

            raw_response = self.llm_client.invoke(prompt)
            generated_code = self._clean_llm_response(raw_response)

            if not generated_code.strip():
                logger.warning("LLM returned an empty response for %s. Skipping file creation.", file_plan.path)
                return

            accumulated_code[file_plan.path] = generated_code

            full_output_path = os.path.join(self.output_directory, file_plan.path)
            self.filesystem_manager.write_file(full_output_path, generated_code)
            logger.info("Successfully generated and saved code for %s", file_plan.path)

        except Exception as e:
            logger.error("Failed to generate or save code for %s. Error: %s", file_plan.path, e, exc_info=True)
            # Continue to attempt generating other files

    def _construct_generation_prompt(
        self,
        project_description: str,
        file_path: str,
        file_purpose: str,
        accumulated_code: Dict[str, str],
    ) -> str:
        """Constructs the prompt for generating a single file's code.

        Args:
            project_description (str): A high-level description of the project.
            file_path (str): The path of the file to be generated.
            file_purpose (str): The specific purpose or responsibility of this file.
            accumulated_code (Dict[str, str]): A dictionary of already generated files
                                               and their content for context.

        Returns:
            str: The fully formatted prompt ready to be sent to the LLM.
        """
        try:
            template = self.prompt_loader.load('generate_code')
        except FileNotFoundError:
            logger.warning("generate_code.prompt template not found. Using a default template.")
            template = self._get_default_prompt_template()

        context_files_str = self._format_context_files(accumulated_code)

        return template.format(
            project_description=project_description,
            file_path=file_path,
            file_purpose=file_purpose,
            context_files=context_files_str,
        )

    def _format_context_files(self, accumulated_code: Dict[str, str]) -> str:
        """Formats the accumulated code into a string for the prompt context.

        Args:
            accumulated_code (Dict[str, str]): Dictionary of file paths to their code.

        Returns:
            str: A formatted string representing the project structure and code.
        """
        if not accumulated_code:
            return "No other files have been generated yet."

        context_parts = ["Here are the other files that have been generated so far for context:"]
        for path, code in accumulated_code.items():
            context_parts.append(f"\n--- File: {path} ---\n```python\n{code}\n```\n")

        return "\n".join(context_parts)

    def _clean_llm_response(self, response: str) -> str:
        """Cleans the raw response from the LLM.

        This method removes common artifacts like markdown code fences (e.g., ```python)
        and leading/trailing whitespace.

        Args:
            response (str): The raw string response from the LLM.

        Returns:
            str: The cleaned code content.
        """
        if not isinstance(response, str):
            logger.warning("LLM response is not a string, but %s. Coercing to string.", type(response))
            response = str(response)

        lines = response.strip().split('\n')

        if lines and lines[0].strip().startswith('```'):
            lines.pop(0)
        if lines and lines[-1].strip() == '```':
            lines.pop(-1)

        return "\n".join(lines).strip()

    def _get_default_prompt_template(self) -> str:
        """Provides a fallback default prompt template if the prompt file is not found.

        Returns:
            str: A default prompt template string.
        """
        return (
            "You are an expert senior Python developer. Your task is to write clean, production-ready Python code "
            "for a specific file based on the project context and file description provided.\n\n"
            "**PROJECT DESCRIPTION**\n"
            "{project_description}\n\n"
            "**CONTEXT: OTHER FILES**\n"
            "This section contains the code for other files in the project that have already been generated. "
            "Use them as context to ensure consistency in coding style, imports, and logic.\n"
            "{context_files}\n\n"
            "**CURRENT FILE TO IMPLEMENT**\n"
            "- File Path: {file_path}\n"
            "- File Purpose: {file_purpose}\n\n"
            "**INSTRUCTIONS**\n"
            "1. Write the complete, executable Python code for the file specified in 'CURRENT FILE TO IMPLEMENT'.\n"
            "2. Do NOT include any placeholders, TODOs, or comments like '# your code here'. The code must be final.\n"
            "3. Adhere to Python best practices, including the use of Google-style docstrings for all public classes and functions.\n"
            "4. Ensure the code is clean, follows SOLID principles, and is well-structured.\n"
            "5. All imports MUST be absolute from the repo root package (e.g., 'from src.codepori.managers...'), never relative.\n"
            "6. Your output should ONLY be the raw code for the file. Do not include any explanatory text, greetings, or markdown fences like ```python.\n\n"
            "Begin writing the code for {file_path} now:\n"
        )
