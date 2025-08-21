import json
import logging
from typing import List, Dict, Any, Optional

from pydantic import BaseModel, Field, ValidationError

from src.codepori.context import Context
from src.codepori.llm.client import LLMClient
from src.codepori.stages.base import Stage, StageExecutionError

# Configure a logger for this module
logger = logging.getLogger(__name__)


class PlanGenerationError(StageExecutionError):
    """Custom exception for errors during the planning stage."""
    pass


class FileImplementationDetail(BaseModel):
    """Model for detailed implementation steps for a single file."""
    step_number: int = Field(
        ...,
        description="Sequential number for the implementation step."
    )
    description: str = Field(
        ...,
        description="Detailed description of what this implementation step entails."
    )


class FilePlan(BaseModel):
    """Defines the plan for a single file to be generated."""
    path: str = Field(
        ...,
        description="The full, relative path to the file from the project root."
    )
    description: str = Field(
        ...,
        description="A comprehensive description of the file's purpose, components, and responsibilities."
    )
    dependencies: List[str] = Field(
        default_factory=list,
        description="A list of other file paths in this plan that this file depends on."
    )
    implementation_steps: List[FileImplementationDetail] = Field(
        ...,
        description="A step-by-step breakdown of how to implement this file."
    )


class DevelopmentPlan(BaseModel):
    """Represents the entire, machine-readable development plan for the project."""
    architecture_summary: str = Field(
        ...,
        description="A high-level summary of the proposed software architecture."
    )
    files: List[FilePlan] = Field(
        ...,
        description="A list of all files to be created for the project."
    )


class PlanStage(Stage):
    """Implements the planning stage of the software development process.

    This stage interacts with an LLM to transform a high-level project description
    into a detailed, machine-readable development plan. The plan is structured
    and validated using Pydantic models before being stored in the shared context
    for subsequent stages.
    """

    # Constants for context keys to avoid magic strings
    CONTEXT_PROJECT_DESCRIPTION_KEY = "project_description"
    CONTEXT_PLAN_KEY = "development_plan"

    def __init__(
        self,
        llm_client: LLMClient,
        max_retries: int = 3,
        llm_temperature: float = 0.2,
        llm_max_tokens: int = 4096,
    ):
        """Initializes the PlanStage.

        Args:
            llm_client: An instance of LLMClient to interact with the language model.
            max_retries: The maximum number of times to retry LLM calls on failure.
            llm_temperature: The temperature setting for the LLM generation.
            llm_max_tokens: The maximum number of tokens for the LLM response.
        """
        if not isinstance(llm_client, LLMClient):
            raise TypeError("llm_client must be an instance of LLMClient")
        if not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")

        self.llm_client = llm_client
        self.max_retries = max_retries
        self.llm_temperature = llm_temperature
        self.llm_max_tokens = llm_max_tokens
        logger.info("PlanStage initialized with %d retries.", self.max_retries)

    def run(self, context: Context) -> Context:
        """Executes the planning stage.

        Retrieves the project description, generates a development plan using the LLM,
        validates it, and stores it in the context.

        Args:
            context: The shared context object containing project information.

        Returns:
            The updated context object with the development plan.

        Raises:
            PlanGenerationError: If the stage fails to generate a valid plan after all retries.
        """
        logger.info("Starting PlanStage execution.")

        project_description = context.get(self.CONTEXT_PROJECT_DESCRIPTION_KEY)
        if not project_description or not isinstance(project_description, str):
            raise PlanGenerationError(
                f"'{self.CONTEXT_PROJECT_DESCRIPTION_KEY}' not found or invalid in context."
            )

        logger.debug("Project description retrieved from context.")

        prompt = self._build_prompt(project_description)

        for attempt in range(self.max_retries):
            logger.info("Attempt %d of %d to generate development plan.", attempt + 1, self.max_retries)
            try:
                raw_response = self.llm_client.generate(
                    prompt=prompt,
                    temperature=self.llm_temperature,
                    max_tokens=self.llm_max_tokens,
                )

                logger.debug("Raw LLM response received.")
                plan = self._parse_and_validate_plan(raw_response)

                if not plan.files:
                     raise ValueError("The generated plan contains no files to implement.")

                context.set(self.CONTEXT_PLAN_KEY, plan)
                logger.info("Successfully generated and validated development plan.")
                logger.debug("Development plan stored in context under key '%s'.", self.CONTEXT_PLAN_KEY)
                return context

            except (json.JSONDecodeError, ValidationError, ValueError) as e:
                logger.warning(
                    "Attempt %d failed: %s. Raw response was: %s",
                    attempt + 1,
                    e,
                    raw_response[:500] + '...' if raw_response else 'EMPTY'
                )
                if attempt == self.max_retries - 1:
                    logger.error("All attempts to generate a valid development plan failed.")
                    raise PlanGenerationError(
                        f"Failed to generate and validate a development plan after {self.max_retries} attempts. Last error: {e}"
                    )
            except Exception as e:
                logger.error("An unexpected error occurred during plan generation on attempt %d: %s", attempt + 1, e, exc_info=True)
                if attempt == self.max_retries - 1:
                    raise PlanGenerationError(f"An unexpected error stopped plan generation: {e}") from e

        # This line should be unreachable, but acts as a safeguard.
        raise PlanGenerationError("Exited retry loop without successfully generating a plan.")

    def _parse_and_validate_plan(self, llm_output: str) -> DevelopmentPlan:
        """Parses the LLM's JSON output and validates it against the Pydantic model.

        Args:
            llm_output: The raw string output from the language model.

        Returns:
            A validated DevelopmentPlan object.

        Raises:
            json.JSONDecodeError: If the output is not valid JSON.
            ValidationError: If the JSON does not conform to the DevelopmentPlan schema.
        """
        logger.debug("Attempting to parse and validate LLM output.")
        # The LLM might wrap the JSON in markdown code fences, so we strip them.
        cleaned_output = llm_output.strip()
        if cleaned_output.startswith('```json'):
            cleaned_output = cleaned_output[7:]
        if cleaned_output.endswith('```'):
            cleaned_output = cleaned_output[:-3]
        cleaned_output = cleaned_output.strip()

        try:
            plan_dict = json.loads(cleaned_output)
            plan = DevelopmentPlan.model_validate(plan_dict)
            logger.debug("LLM output successfully parsed and validated.")
            return plan
        except json.JSONDecodeError as e:
            logger.error("Failed to decode JSON from LLM output: %s", e)
            raise
        except ValidationError as e:
            logger.error("Failed to validate plan against Pydantic model: %s", e)
            raise

    def _build_prompt(self, project_description: str) -> str:
        """Constructs a detailed prompt for the LLM to generate a development plan.

        This method uses a few-shot prompting technique by providing the JSON schema
        and a clear example to guide the LLM towards the correct output format.

        Args:
            project_description: The user's high-level description of the project.

        Returns:
            The fully formatted prompt string.
        """
        # Generate the JSON schema from the Pydantic model
        schema = DevelopmentPlan.model_json_schema()

        prompt = f"""You are a senior software architect responsible for creating a detailed development plan.
Based on the user's project description, you must generate a comprehensive, machine-readable plan in JSON format.

**Project Description:**
---
{project_description}
---

**Instructions:**
1.  Analyze the project description to understand the core requirements, features, and components.
2.  Propose a high-level software architecture. This should be a brief summary.
3.  Break down the project into a list of individual files that need to be created.
4.  For each file, provide a full path, a detailed description of its purpose, a list of its internal dependencies (other files in the plan), and a step-by-step implementation guide.
5.  Your entire output MUST be a single JSON object that strictly adheres to the following JSON schema.
6.  Do not include any text, comments, or explanations outside of the JSON object.

**JSON Schema to Follow:**
```json
{json.dumps(schema, indent=2)}
```

**Example Output Structure:**
```json
{{
  "architecture_summary": "A simple web server using Flask with a single endpoint.",
  "files": [
    {{
      "path": "src/main.py",
      "description": "The main entry point of the application. Initializes and runs the Flask server.",
      "dependencies": [],
      "implementation_steps": [
        {{
          "step_number": 1,
          "description": "Import Flask and create an application instance."
        }},
        {{
          "step_number": 2,
          "description": "Define a single route '/' that returns 'Hello, World!'."
        }},
        {{
          "step_number": 3,
          "description": "Add a main execution block to run the development server."
        }}
      ]
    }},
    {{
      "path": "requirements.txt",
      "description": "Lists the Python package dependencies for the project.",
      "dependencies": [],
      "implementation_steps": [
        {{
          "step_number": 1,
          "description": "Add the 'Flask' library to the file."
        }}
      ]
    }}
  ]
}}
```

Now, generate the complete JSON output for the provided project description.

**JSON Output:**
"""
        logger.debug("Plan generation prompt created.")
        return prompt
