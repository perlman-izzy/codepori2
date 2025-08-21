# Copyright (c) 2024, CodePori. All rights reserved.
"""Configuration loading and validation utility for the CodePori driver.

This module provides a robust mechanism for loading, validating, and accessing
configuration settings from a 'config.yaml' file. It uses Pydantic for data
validation and model management, ensuring that the configuration is well-structured
and type-safe. The module also supports overriding sensitive information, such as
API keys, through environment variables.

A singleton pattern is implemented using a cached function `get_config()` to ensure
that the configuration is loaded and parsed only once per application lifecycle.

Key Features:
- Automatic discovery of 'config.yaml' by searching up from the current directory.
- Validation of configuration structure and types using Pydantic models.
- Support for environment variable overrides for specific settings (e.g., API keys).
- Centralized, cached access to configuration settings via `get_config()`.
- Custom exceptions for clear error handling of configuration issues.
- Path resolution relative to the project root for consistency.

Example `config.yaml` structure:

    project:
      name: "My Awesome Project"
      version: "0.1.0"

    paths:
      output_dir: "build/output"
      templates_dir: "assets/templates"
      logs_dir: "logs"

    llm:
      provider: "openai"
      model_name: "gpt-4-turbo-preview"
      temperature: 0.7
      max_tokens: 4096
      # api_key can be set here or via CODEPORI_LLM_API_KEY environment variable
      # api_key: "sk-..."

Usage:

    from src.codepori.utils.config import get_config, ConfigError

    try:
        config = get_config()
        print(f"Project Name: {config.project.name}")
        print(f"LLM Model: {config.llm.model_name}")
        # Use resolved, absolute paths
        print(f"Output Directory: {config.resolved_output_dir}")
    except ConfigError as e:
        print(f"Error loading configuration: {e}")

"""

import logging
import os
from pathlib import Path
from typing import Optional, Union, Any, Dict

import yaml
from pydantic import BaseModel, Field, ValidationError, SecretStr, ConfigDict
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- Constants ---
CONFIG_FILE_NAME = "config.yaml"
MAX_SEARCH_DEPTH = 5

# --- Module-level Logger ---
logger = logging.getLogger(__name__)


# --- Custom Exceptions ---
class ConfigError(Exception):
    """Base exception for all configuration-related errors."""
    pass


class ConfigNotFoundError(ConfigError, FileNotFoundError):
    """Raised when the configuration file cannot be found."""

    def __init__(self, message: str):
        super().__init__(message)


class InvalidConfigError(ConfigError, ValueError):
    """Raised when the configuration file is malformed or invalid."""

    def __init__(self, message: str, pydantic_error: Optional[ValidationError] = None):
        self.pydantic_error = pydantic_error
        super().__init__(message)


# --- Pydantic Configuration Models ---

class LLMConfig(BaseSettings):
    """Defines the configuration for the Language Model.

    This model uses `pydantic-settings` to allow overriding values from
    environment variables. For example, `api_key` can be set by the
    `CODEPORI_LLM_API_KEY` environment variable.
    """
    model_config = SettingsConfigDict(env_prefix="CODEPORI_LLM_", extra='forbid')

    provider: str = Field(
        "openai",
        description="The provider of the language model (e.g., 'openai', 'anthropic')."
    )
    model_name: str = Field(
        ...,
        description="The specific model name to use (e.g., 'gpt-4-turbo-preview')."
    )
    api_key: Optional[SecretStr] = Field(
        None,
        description="API key for the LLM provider. Best set via environment variable."
    )
    temperature: float = Field(
        0.7,
        ge=0.0, le=2.0,
        description="Controls randomness. Lower is more deterministic."
    )
    max_tokens: int = Field(
        4096,
        gt=0,
        description="The maximum number of tokens to generate in a completion."
    )


class PathsConfig(BaseModel):
    """Defines the standard paths used by the application."""
    model_config = ConfigDict(extra='forbid')

    output_dir: str = Field(
        "output",
        description="Directory for generated code and artifacts, relative to project root."
    )
    templates_dir: str = Field(
        "templates",
        description="Directory containing code templates, relative to project root."
    )
    logs_dir: str = Field(
        "logs",
        description="Directory for storing log files, relative to project root."
    )


class ProjectConfig(BaseModel):
    """Defines metadata about the user's project."""
    model_config = ConfigDict(extra='forbid')

    name: str = Field(
        ...,
        description="The name of the software project being developed."
    )
    version: str = Field(
        "0.1.0",
        description="The current version of the software project."
    )


class CodeporiConfig(BaseModel):
    """The root model for the entire application configuration."""
    model_config = ConfigDict(extra='forbid')

    project: ProjectConfig = Field(..., description="Project metadata.")
    paths: PathsConfig = Field(..., description="Application file paths.")
    llm: LLMConfig = Field(..., description="Language Model settings.")

    # This field is populated dynamically after loading and is not part of the YAML.
    _root_dir: Path = Field(..., exclude=True)

    @property
    def root_dir(self) -> Path:
        """Gets the project root directory (where config.yaml was found)."""
        return self._root_dir

    @property
    def resolved_output_dir(self) -> Path:
        """Gets the absolute path to the output directory."""
        return (self._root_dir / self.paths.output_dir).resolve()

    @property
    def resolved_templates_dir(self) -> Path:
        """Gets the absolute path to the templates directory."""
        return (self._root_dir / self.paths.templates_dir).resolve()

    @property
    def resolved_logs_dir(self) -> Path:
        """Gets the absolute path to the logs directory."""
        return (self._root_dir / self.paths.logs_dir).resolve()


# --- Module-level Cache ---
_config_cache: Optional[CodeporiConfig] = None


# --- Core Logic (Private) ---

def _find_config_file() -> Path:
    """Searches for config.yaml upwards from the current directory.

    Starts from the current working directory and traverses up a maximum of
    `MAX_SEARCH_DEPTH` parent directories to find `config.yaml`.

    Returns:
        The absolute path to the found `config.yaml` file.

    Raises:
        ConfigNotFoundError: If the file is not found within the search depth.
    """
    current_dir = Path.cwd().resolve()
    for _ in range(MAX_SEARCH_DEPTH):
        potential_path = current_dir / CONFIG_FILE_NAME
        if potential_path.is_file():
            logger.info(f"Configuration file found at: {potential_path}")
            return potential_path
        if current_dir.parent == current_dir:  # Reached the root directory
            break
        current_dir = current_dir.parent

    raise ConfigNotFoundError(
        f"'{CONFIG_FILE_NAME}' not found in current directory or any parent up to {MAX_SEARCH_DEPTH} levels."
    )


def _load_and_validate_config(config_path: Path) -> CodeporiConfig:
    """Loads, parses, and validates the configuration file.

    Args:
        config_path: The path to the configuration file.

    Returns:
        A validated instance of the CodeporiConfig model.

    Raises:
        ConfigNotFoundError: If the specified config_path does not exist.
        InvalidConfigError: If the file is not valid YAML or fails Pydantic validation.
    """
    if not config_path.is_file():
        raise ConfigNotFoundError(f"Configuration file not found at specified path: {config_path}")

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config_data = yaml.safe_load(f)
        if not isinstance(config_data, dict):
            raise InvalidConfigError("Config file content is not a valid YAML dictionary.")
    except yaml.YAMLError as e:
        raise InvalidConfigError(f"Error parsing YAML file '{config_path}': {e}") from e

    try:
        # First, validate the structure from the YAML file
        config_model = CodeporiConfig.model_validate(config_data)

        # Dynamically set the project root directory based on the config file's location
        config_model._root_dir = config_path.parent.resolve()

        return config_model

    except ValidationError as e:
        error_message = f"Configuration validation failed for '{config_path}':\n{e}"
        logger.error(error_message)
        raise InvalidConfigError(error_message, pydantic_error=e) from e


# --- Public Interface ---

def get_config(config_path: Optional[Union[str, Path]] = None) -> CodeporiConfig:
    """Gets the application configuration, loading it if necessary.

    This function acts as the main entry point for accessing configuration.
    It implements a singleton pattern by caching the configuration after the
    first successful load. Subsequent calls will return the cached object,
    avoiding repeated file I/O and validation.

    The function can either automatically discover the `config.yaml` file or
    load it from an explicitly provided path.

    Args:
        config_path: An optional path to a specific `config.yaml` file.
            If None, the function will search for the file automatically.

    Returns:
        The fully validated and immutable application configuration object.

    Raises:
        ConfigError: A base class for more specific configuration errors like
            `ConfigNotFoundError` or `InvalidConfigError`.
    """
    global _config_cache
    if _config_cache is not None and config_path is None:
        return _config_cache

    try:
        if config_path:
            path_to_load = Path(config_path).resolve()
        else:
            path_to_load = _find_config_file()

        loaded_config = _load_and_validate_config(path_to_load)

        if config_path is None:  # Only cache if not using an explicit path
            _config_cache = loaded_config

        return loaded_config

    except (ConfigNotFoundError, InvalidConfigError):
        # Re-raise known errors directly
        raise
    except Exception as e:
        # Catch any other unexpected errors during loading
        message = f"An unexpected error occurred while loading configuration: {e}"
        logger.exception(message)
        raise ConfigError(message) from e
