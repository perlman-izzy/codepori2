# Jules Setup Script

A comprehensive, multi-stack project detection and setup script for fresh Ubuntu VM environments.

## Overview

The Jules setup script (`jules-setup.sh`) automatically detects and sets up development environments for multiple technology stacks:

- **Python** - with virtual environments, dependency management, and testing
- **Node.js** - with package manager detection and build/test execution  
- **.NET** - with SDK installation, dependency restoration, and testing
- **Java** - with Gradle/Maven support and testing
- **Go** - with module management and testing

## Features

### Multi-Stack Detection
- Automatically detects project types based on configuration files and source code
- Supports multiple stacks in the same project
- Robust detection logic handles edge cases and unusual project structures

### Smart Dependency Management
- **Python**: Supports requirements.txt, pyproject.toml, or minimal pytest-only setup
- **Node.js**: Automatically chooses between npm, yarn, or pnpm based on lock files
- **.NET**: Handles project restoration and NuGet packages
- **Java**: Works with Gradle (including wrapper), Maven, or standalone builds
- **Go**: Uses go mod for dependency management

### Intelligent Testing
- Automatically detects and runs tests for each stack
- **Python**: Finds test files, test directories, and runs pytest
- **Node.js**: Executes npm test scripts if available
- **.NET**: Discovers and runs test projects
- **Java**: Runs Gradle or Maven test targets
- **Go**: Detects and runs Go test files

### Additional Capabilities
- **Automatic Dependency Scanning**: Analyzes source code to detect missing dependencies
- **Compilation Verification**: Checks that all source files compile successfully  
- **Build Automation**: Runs build steps where applicable
- **Tool Version Reporting**: Shows versions of all installed development tools
- **Robust Error Handling**: Continues setup even if individual steps fail

## Usage

### Basic Usage
```bash
sudo ./jules-setup.sh
```

### Environment Variables
The script respects these environment variables:
- `DEBIAN_FRONTEND=noninteractive` (automatically set)
- `PIP_DISABLE_PIP_VERSION_CHECK=1` (automatically set)  
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` (automatically set)

### Requirements
- Fresh Ubuntu VM (tested on Ubuntu 20.04+)
- sudo/root access for system package installation
- Internet connectivity for package downloads

## Detection Logic

### Python Detection
Detects Python projects if any of:
- `requirements.txt` exists
- `pyproject.toml` exists  
- Any `.py` files exist (up to 2 directory levels deep)

### Node.js Detection  
Detects Node.js projects if:
- `package.json` exists

### .NET Detection
Detects .NET projects if any of:
- `*.csproj` files exist (up to 2 directory levels deep)
- `*.sln` solution files exist (up to 2 directory levels deep)

### Java Detection
Detects Java projects if any of:
- `gradlew` (Gradle wrapper) exists
- `build.gradle` exists
- `pom.xml` (Maven) exists

### Go Detection
Detects Go projects if:
- `go.mod` exists

## Setup Process

### Python Setup
1. Creates virtual environment (prefers `uv` if available, falls back to `venv`)
2. Upgrades pip in virtual environment
3. Installs dependencies from requirements.txt or pyproject.toml
4. Scans source code for additional dependencies (e.g., requests)  
5. Compiles all Python files to check for syntax errors
6. Runs pytest if test files are found

### Node.js Setup
1. Installs Node.js and npm if not available
2. Detects package manager (yarn > pnpm > npm)
3. Installs dependencies with appropriate package manager
4. Runs build script if available
5. Runs test script if available

### .NET Setup
1. Installs .NET SDK 8.0 if not available
2. Restores NuGet packages with `dotnet restore`
3. Builds project with `dotnet build`
4. Runs tests if test projects exist

### Java Setup
1. Installs OpenJDK if not available
2. Uses Gradle wrapper if available, otherwise Gradle or Maven
3. Runs clean build process
4. Executes test suite

### Go Setup
1. Installs Go compiler if not available
2. Runs `go mod tidy` to manage dependencies
3. Builds all packages with `go build ./...`
4. Runs tests if test files exist

## Output

The script provides detailed logging throughout execution and ends with:
- Tool version summary
- Success message: "Setup completed successfully"
- Status indicator: "JULES_OK"

## Testing

The script has been thoroughly tested with:
- Multiple project types and configurations
- Edge cases (empty directories, broken files, unusual names)
- Integration with real repositories
- Error conditions and recovery scenarios

## Integration

Successfully integrates with existing Python codebases including:
- CodePori pipeline projects
- Flask applications  
- Test suites using pytest
- Projects with complex dependency graphs

---

*This script was designed for the CodePori project and has been validated against real-world Python development environments.*