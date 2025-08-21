import logging
import os
import shutil
from pathlib import Path
from typing import List

# Configure a logger for this module
logger = logging.getLogger(__name__)


class FileSystemError(IOError):
    """Base exception for file system operations in this module."""
    pass


class FileCreationError(FileSystemError):
    """Raised when a file cannot be created."""
    pass


class DirectoryCreationError(FileSystemError):
    """Raised when a directory cannot be created."""
    pass


class FileReadError(FileSystemError):
    """Raised when a file cannot be read."""
    pass


class FileDeleteError(FileSystemError):
    """Raised when a file cannot be deleted."""
    pass


class DirectoryDeleteError(FileSystemError):
    """Raised when a directory cannot be deleted."""
    pass


class PathTraversalError(FileSystemError):
    """Raised on detected path traversal attempts."""
    pass


class FileSystemManager:
    """Manages all file system operations within a specific base directory.

    This class provides a centralized and secure interface for interacting with the
    file system, scoped to a designated root directory. It handles creation,
    reading, writing, and deletion of files and directories, ensuring that all
    operations are contained within the base path to prevent path traversal issues.

    Attributes:
        base_path (Path): The absolute path to the root directory for all operations.
    """

    def __init__(self, base_directory: str = "output/code"):
        """Initializes the FileSystemManager.

        Args:
            base_directory (str): The relative or absolute path to the directory
                to be managed. Defaults to 'output/code'. It will be created
                if it does not exist.

        Raises:
            DirectoryCreationError: If the base directory cannot be created.
        """
        try:
            self.base_path = Path(base_directory).resolve()
            logger.info(f"Initializing FileSystemManager with base path: {self.base_path}")
            self.base_path.mkdir(parents=True, exist_ok=True)
            logger.info(f"Base directory '{self.base_path}' is ready.")
        except (OSError, PermissionError) as e:
            logger.error(f"Failed to create or access base directory '{self.base_path}': {e}")
            raise DirectoryCreationError(
                f"Could not create or access the base directory: {self.base_path}"
            ) from e

    def _resolve_safe_path(self, relative_path: str) -> Path:
        """Resolves a relative path to an absolute path within the base directory.

        This method ensures that the resolved path is strictly within the managed
        base directory, preventing path traversal attacks (e.g., using '..').

        Args:
            relative_path (str): The relative path from the base directory.

        Returns:
            Path: The resolved, absolute, and safe path object.

        Raises:
            PathTraversalError: If the resolved path is outside the base directory.
        """
        if os.path.isabs(relative_path):
            logger.warning(f"Absolute path provided '{relative_path}'. It will be treated as relative to the base.")
            # Strip leading slashes to treat it as a relative path
            relative_path = relative_path.lstrip('/\\')

        # Join and resolve the path to handle '..' etc.
        absolute_path = (self.base_path / relative_path).resolve()

        # Security check: ensure the resolved path is a sub-path of the base_path
        if self.base_path not in absolute_path.parents and absolute_path != self.base_path:
            logger.error(
                f"Path traversal attempt detected. Resolved path '{absolute_path}' "
                f"is outside of base directory '{self.base_path}'."
            )
            raise PathTraversalError(
                f"Access to '{relative_path}' is denied. Path is outside the managed directory."
            )

        return absolute_path

    def create_directory(self, dir_path: str, exist_ok: bool = True) -> Path:
        """Creates a directory within the base directory.

        Args:
            dir_path (str): The relative path of the directory to create.
            exist_ok (bool): If True, no error is raised if the directory already exists.

        Returns:
            Path: The absolute path of the created directory.

        Raises:
            DirectoryCreationError: If the directory could not be created.
        """
        try:
            safe_path = self._resolve_safe_path(dir_path)
            logger.info(f"Creating directory: {safe_path}")
            safe_path.mkdir(parents=True, exist_ok=exist_ok)
            return safe_path
        except (OSError, PermissionError) as e:
            logger.error(f"Failed to create directory '{dir_path}': {e}")
            raise DirectoryCreationError(f"Could not create directory '{dir_path}'.") from e

    def write_file(self, file_path: str, content: str, overwrite: bool = True) -> Path:
        """Writes content to a file within the base directory.

        This method will create any necessary parent directories for the file.

        Args:
            file_path (str): The relative path of the file to write.
            content (str): The string content to write to the file.
            overwrite (bool): If False and the file exists, a FileCreationError is raised.

        Returns:
            Path: The absolute path of the written file.

        Raises:
            FileCreationError: If the file could not be written, or if overwrite is
                False and the file already exists.
        """
        safe_path = self._resolve_safe_path(file_path)

        if not overwrite and safe_path.exists():
            logger.error(f"Attempted to write to existing file '{safe_path}' with overwrite=False.")
            raise FileCreationError(f"File '{file_path}' already exists and overwrite is set to False.")

        try:
            logger.info(f"Writing file: {safe_path}")
            # Ensure parent directory exists
            safe_path.parent.mkdir(parents=True, exist_ok=True)
            with open(safe_path, 'w', encoding='utf-8') as f:
                f.write(content)
            logger.debug(f"Successfully wrote {len(content)} bytes to {safe_path}")
            return safe_path
        except (OSError, PermissionError, TypeError) as e:
            logger.error(f"Failed to write file '{file_path}': {e}")
            raise FileCreationError(f"Could not write to file '{file_path}'.") from e

    def read_file(self, file_path: str) -> str:
        """Reads the content of a file from within the base directory.

        Args:
            file_path (str): The relative path of the file to read.

        Returns:
            str: The content of the file.

        Raises:
            FileReadError: If the file does not exist or cannot be read.
        """
        safe_path = self._resolve_safe_path(file_path)

        if not safe_path.is_file():
            logger.error(f"File not found at path: {safe_path}")
            raise FileReadError(f"File '{file_path}' does not exist or is not a regular file.")

        try:
            logger.info(f"Reading file: {safe_path}")
            with open(safe_path, 'r', encoding='utf-8') as f:
                content = f.read()
            return content
        except (OSError, PermissionError, UnicodeDecodeError) as e:
            logger.error(f"Failed to read file '{file_path}': {e}")
            raise FileReadError(f"Could not read file '{file_path}'.") from e

    def file_exists(self, file_path: str) -> bool:
        """Checks if a file exists within the base directory.

        Args:
            file_path (str): The relative path of the file to check.

        Returns:
            bool: True if the file exists, False otherwise.
        """
        try:
            safe_path = self._resolve_safe_path(file_path)
            return safe_path.is_file()
        except PathTraversalError:
            return False

    def list_files(self, dir_path: str = ".", recursive: bool = False) -> List[str]:
        """Lists files in a given directory within the base path.

        Args:
            dir_path (str): The relative directory path to search in. Defaults to the base path.
            recursive (bool): If True, lists files in subdirectories as well.

        Returns:
            List[str]: A list of relative file paths.
        """
        safe_dir_path = self._resolve_safe_path(dir_path)
        if not safe_dir_path.is_dir():
            logger.warning(f"Attempted to list files in non-existent directory: {safe_dir_path}")
            return []

        pattern = "**/*" if recursive else "*"
        files = [
            str(p.relative_to(self.base_path)) 
            for p in safe_dir_path.glob(pattern) 
            if p.is_file()
        ]
        return files

    def delete_file(self, file_path: str) -> None:
        """Deletes a file within the base directory.

        Args:
            file_path (str): The relative path of the file to delete.

        Raises:
            FileDeleteError: If the file could not be deleted.
        """
        safe_path = self._resolve_safe_path(file_path)
        if not safe_path.is_file():
            logger.warning(f"Attempted to delete non-existent file: {safe_path}")
            # Idempotent: if it doesn't exist, it's already 'deleted'.
            return

        try:
            logger.info(f"Deleting file: {safe_path}")
            safe_path.unlink()
        except (OSError, PermissionError) as e:
            logger.error(f"Failed to delete file '{file_path}': {e}")
            raise FileDeleteError(f"Could not delete file '{file_path}'.") from e

    def delete_directory(self, dir_path: str) -> None:
        """Deletes a directory and all its contents recursively.

        Args:
            dir_path (str): The relative path of the directory to delete.

        Raises:
            DirectoryDeleteError: If the directory could not be deleted.
        """
        safe_path = self._resolve_safe_path(dir_path)
        if not safe_path.is_dir():
            logger.warning(f"Attempted to delete non-existent directory: {safe_path}")
            return

        if safe_path == self.base_path:
            logger.error("Attempted to delete the base directory itself. Operation aborted.")
            raise DirectoryDeleteError("Cannot delete the base directory of the manager.")

        try:
            logger.info(f"Recursively deleting directory: {safe_path}")
            shutil.rmtree(safe_path)
        except (OSError, PermissionError) as e:
            logger.error(f"Failed to delete directory '{dir_path}': {e}")
            raise DirectoryDeleteError(f"Could not delete directory '{dir_path}'.") from e
