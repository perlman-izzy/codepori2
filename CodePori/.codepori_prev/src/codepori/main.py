import argparse
import logging
import sys
import yaml
from pathlib import Path
from typing import Any, Dict, List, Type, Optional

__version__ = "0.1.0"

# ==============================================================================
# Scaffolding Components (to be moved to their respective modules)
# ==============================================================================

class LLMManager:
    """Manages interactions with the Language Model.

    This class is a placeholder for a more complex system that would handle
    API calls, manage different models, and format prompts and responses.
    For the purpose of this driver, it serves as a dependency to be injected
    into the pipeline stages.

    Args:
        config (Dict[str, Any]): Configuration specific to the LLM, such as
            model name, API key (in a real scenario), temperature, etc.
    """
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.model_name = self.config.get("model", "dummy-model")
        logging.info("LLMManager initialized for model: %s", self.model_name)

    def generate(self, prompt: str) -> str:
        """Simulates generating a response from the LLM.

        Args:
            prompt (str): The input prompt for the model.

        Returns:
            str: A simulated response from the model.
        """
        logging.debug("Generating LLM response for prompt: %s", prompt[:100])
        return f"# Simulated response for: {prompt[:50]}...\nprint('Hello from {self.model_name}')"

class ProjectContext:
    """Holds and manages the state of the project being generated.

    This object is passed through the pipeline from one stage to the next,
    accumulating data like project structure, file contents, and other
    metadata.

    Args:
        initial_prompt (str): The root prompt describing the project.
        output_dir (Path): The root directory for the generated project.
    """
    def __init__(self, initial_prompt: str, output_dir: Path) -> None:
        self.initial_prompt = initial_prompt
        self.output_dir = output_dir
        self.data: Dict[str, Any] = {}
        logging.info("ProjectContext created for output directory: '%s'", self.output_dir)

    def set(self, key: str, value: Any) -> None:
        """Sets a value in the context's data store."""
        self.data[key] = value
        logging.debug("Context key '%s' set.", key)

    def get(self, key: str, default: Optional[Any] = None) -> Any:
        """Gets a value from the context's data store."""
        return self.data.get(key, default)


class Stage:
    """Abstract base class for a pipeline stage."""

    def __init__(self, llm_manager: LLMManager, config: Dict[str, Any]) -> None:
        """Initializes the stage.

        Args:
            llm_manager (LLMManager): The shared LLM manager instance.
            config (Dict[str, Any]): Stage-specific configuration.
        """
        self.llm_manager = llm_manager
        self.config = config

    def run(self, context: ProjectContext) -> ProjectContext:
        """Executes the logic for this stage.

        This method must be implemented by subclasses.

        Args:
            context (ProjectContext): The current project context.

        Returns:
            ProjectContext: The modified project context.

        Raises:
            NotImplementedError: If the subclass does not implement this method.
        """
        raise NotImplementedError("Subclasses must implement the 'run' method.")

class ProjectSetupStage(Stage):
    """A pipeline stage to set up the basic project directory structure."""

    def run(self, context: ProjectContext) -> ProjectContext:
        """Creates the output directory and a basic README file.

        Args:
            context (ProjectContext): The current project context.

        Returns:
            ProjectContext: The context, updated after execution.
        """
        logging.info("Executing ProjectSetupStage...")
        try:
            context.output_dir.mkdir(parents=True, exist_ok=True)
            readme_content = f"# Project generated from prompt\n\n`{context.initial_prompt}`"
            (context.output_dir / "README.md").write_text(readme_content)
            logging.info("Project directory and README.md created at '%s'", context.output_dir)
        except OSError as e:
            logging.error("Failed to create project directory: %s", e)
            raise
        return context

class CodeGenerationStage(Stage):
    """A pipeline stage for generating project source code."""

    def run(self, context: ProjectContext) -> ProjectContext:
        """Generates a simple 'main.py' file using the LLM manager.

        Args:
            context (ProjectContext): The current project context.

        Returns:
            ProjectContext: The context, updated after execution.
        """
        logging.info("Executing CodeGenerationStage...")
        src_dir = context.output_dir / "src"
        try:
            src_dir.mkdir(exist_ok=True)
            prompt = f"Create a simple python entrypoint for a project about: {context.initial_prompt}"
            main_py_content = self.llm_manager.generate(prompt)
            (src_dir / "main.py").write_text(main_py_content)
            logging.info("Generated 'src/main.py' with dummy content.")
        except OSError as e:
            logging.error("Failed to create source directory or file: %s", e)
            raise
        return context


STAGE_REGISTRY: Dict[str, Type[Stage]] = {
    "project_setup": ProjectSetupStage,
    "code_generation": CodeGenerationStage,
}

class Pipeline:
    """Represents and executes a sequence of stages.

    Args:
        stages (List[Stage]): An ordered list of stage instances to execute.
    """
    def __init__(self, stages: List[Stage]) -> None:
        if not stages:
            raise ValueError("Pipeline must have at least one stage.")
        self.stages = stages
        logging.info("Pipeline initialized with %d stages.", len(self.stages))

    def execute(self, initial_prompt: str, output_dir: Path) -> ProjectContext:
        """Runs the pipeline from start to finish.

        Args:
            initial_prompt (str): The user's initial project prompt.
            output_dir (Path): The directory to generate the project in.

        Returns:
            ProjectContext: The final context after all stages have run.
        """
        logging.info("Pipeline execution started.")
        context = ProjectContext(initial_prompt, output_dir)
        for i, stage in enumerate(self.stages):
            stage_name = stage.__class__.__name__
            logging.info("--- Running stage %d/%d: %s ---", i + 1, len(self.stages), stage_name)
            try:
                context = stage.run(context)
            except Exception as e:
                logging.error("Pipeline failed at stage '%s': %s", stage_name, e, exc_info=True)
                raise
            logging.info("--- Completed stage %d/%d: %s ---", i + 1, len(self.stages), stage_name)
        logging.info("Pipeline execution finished successfully.")
        return context

# ==============================================================================
# Core Application Logic
# ==============================================================================

DEFAULT_CONFIG_PATH = Path("codepori_config.yaml")

def setup_logging(level: str) -> None:
    """Configures the root logger for the application.

    Args:
        level (str): The logging level (e.g., 'INFO', 'DEBUG').
    """
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - [%(levelname)s] - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

def parse_arguments() -> argparse.Namespace:
    """Parses command-line arguments for the application.

    Returns:
        argparse.Namespace: The parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description="CodePori: An AI-powered project scaffolding tool.",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "prompt",
        type=str,
        help="The initial prompt describing the software project to generate."
    )
    parser.add_argument(
        "-c", "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to the configuration file. Defaults to ./{DEFAULT_CONFIG_PATH}"
    )
    parser.add_argument(
        "-o", "--output-dir",
        type=Path,
        default=None,
        help="Path to the output directory for the generated project. Overrides config."
    )
    parser.add_argument(
        "-l", "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set the logging verbosity."
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}"
    )
    return parser.parse_args()

def load_configuration(config_path: Path) -> Dict[str, Any]:
    """Loads the YAML configuration file.

    Args:
        config_path (Path): The path to the configuration file.

    Returns:
        Dict[str, Any]: The loaded configuration as a dictionary.

    Raises:
        FileNotFoundError: If the config file does not exist.
        yaml.YAMLError: If the config file is not valid YAML.
    """
    logging.info("Loading configuration from: %s", config_path)
    if not config_path.is_file():
        raise FileNotFoundError(f"Configuration file not found at: {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        try:
            return yaml.safe_load(f)
        except yaml.YAMLError as e:
            logging.error("Error parsing YAML configuration: %s", e)
            raise

def assemble_pipeline(config: Dict[str, Any], llm_manager: LLMManager) -> Pipeline:
    """Constructs the pipeline from the configuration.

    Args:
        config (Dict[str, Any]): The application configuration.
        llm_manager (LLMManager): The LLM manager to be used by stages.

    Returns:
        Pipeline: The assembled pipeline instance.

    Raises:
        ValueError: If the pipeline configuration is missing or invalid.
    """
    pipeline_config = config.get("pipeline")
    if not pipeline_config or not isinstance(pipeline_config, list):
        raise ValueError("Configuration must contain a 'pipeline' list.")

    stages = []
    for stage_conf in pipeline_config:
        stage_name = stage_conf.get("name")
        stage_params = stage_conf.get("params", {})
        if not stage_name:
            raise ValueError("Each stage in the pipeline must have a 'name'.")

        stage_class = STAGE_REGISTRY.get(stage_name)
        if not stage_class:
            raise ValueError(f"Unknown stage '{stage_name}'. Available stages: {list(STAGE_REGISTRY.keys())}")

        stage_instance = stage_class(llm_manager, stage_params)
        stages.append(stage_instance)
        logging.debug("Assembled stage '%s'", stage_name)

    return Pipeline(stages)

def main() -> None:
    """The main entry point for the CodePori application."""
    try:
        args = parse_arguments()
        setup_logging(args.log_level)

        logging.info("Starting CodePori v%s", __version__)

        config = load_configuration(args.config)

        output_dir = args.output_dir or Path(config.get("project", {}).get("output_dir", "./codepori_output"))

        llm_manager_config = config.get("llm", {})
        llm_manager = LLMManager(llm_manager_config)

        pipeline = assemble_pipeline(config, llm_manager)

        pipeline.execute(args.prompt, output_dir)

        logging.info("Project generation complete in directory: %s", output_dir.resolve())

    except FileNotFoundError as e:
        logging.error("Configuration Error: %s", e)
        logging.error("Please ensure the config file exists or specify one with -c.")
        sys.exit(1)
    except (ValueError, yaml.YAMLError) as e:
        logging.error("Invalid Configuration: %s", e)
        sys.exit(1)
    except Exception as e:
        logging.critical("An unexpected error occurred: %s", e, exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
