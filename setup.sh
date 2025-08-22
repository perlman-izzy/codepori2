#!/usr/bin/env bash
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1

# Install minimal system dependencies
echo "=== Installing system dependencies ==="
if apt-get update && apt-get install -y build-essential python3-dev libssl-dev libffi-dev pkg-config git ca-certificates; then
    echo "✓ System dependencies installed successfully"
else
    echo "⚠ Failed to install system dependencies (may not have root access), continuing..."
fi

# Detect project stack
PYTHON_DETECTED=0
NODEJS_DETECTED=0
DOTNET_DETECTED=0
JAVA_DETECTED=0
GO_DETECTED=0

echo "=== Detecting project stack ==="

# Check for Python
if [[ -f "requirements.txt" || -f "pyproject.toml" || $(find . -name "*.py" -type f | head -1) ]]; then
    PYTHON_DETECTED=1
    echo "✓ Python project detected"
fi

# Check for Node.js
if [[ -f "package.json" ]]; then
    NODEJS_DETECTED=1
    echo "✓ Node.js project detected"
fi

# Check for .NET
if [[ $(find . -name "*.csproj" -o -name "*.sln" | head -1) ]]; then
    DOTNET_DETECTED=1
    echo "✓ .NET project detected"
fi

# Check for Java
if [[ -f "gradlew" || -f "build.gradle" || -f "pom.xml" ]]; then
    JAVA_DETECTED=1
    echo "✓ Java project detected"
fi

# Check for Go
if [[ -f "go.mod" ]]; then
    GO_DETECTED=1
    echo "✓ Go project detected"
fi

# Python setup
if [[ $PYTHON_DETECTED -eq 1 ]]; then
    echo "=== Setting up Python environment ==="
    
    # Try to use uv if available, else use venv
    if command -v uv &> /dev/null; then
        echo "Using uv for Python virtual environment"
        uv venv venv
        source venv/bin/activate
        
        # Install dependencies
        if [[ -f "pyproject.toml" ]]; then
            echo "Installing dependencies from pyproject.toml"
            uv pip install -e . || echo "⚠ Failed to install from pyproject.toml, continuing..."
        elif [[ -f "requirements.txt" ]]; then
            echo "Installing dependencies from requirements.txt"
            uv pip install -r requirements.txt || echo "⚠ Failed to install from requirements.txt, continuing..."
        else
            echo "No requirements found, installing pytest only"
            uv pip install pytest || echo "⚠ Failed to install pytest, continuing..."
        fi
    else
        echo "Using venv for Python virtual environment"
        python3 -m venv venv
        source venv/bin/activate
        
        # Install dependencies
        if [[ -f "pyproject.toml" ]]; then
            echo "Installing dependencies from pyproject.toml"
            pip install -e . || echo "⚠ Failed to install from pyproject.toml, continuing..."
        elif [[ -f "requirements.txt" ]]; then
            echo "Installing dependencies from requirements.txt"
            pip install -r requirements.txt || echo "⚠ Failed to install from requirements.txt, continuing..."
        else
            echo "No requirements found, installing pytest only"
            pip install pytest || echo "⚠ Failed to install pytest, continuing..."
        fi
    fi
    
    # Compile all Python files
    echo "Compiling Python files"
    python -m compileall . || true
    
    # Run pytest if tests exist
    if [[ -d "tests" || $(find . -maxdepth 2 -name "test_*.py" -o -name "*_test.py" | grep -v "/venv/" | head -1) ]]; then
        echo "Running pytest"
        python -m pytest -v || echo "Tests failed but continuing"
    else
        echo "No tests found, skipping pytest"
    fi
fi

# Node.js setup
if [[ $NODEJS_DETECTED -eq 1 ]]; then
    echo "=== Setting up Node.js environment ==="
    
    # Determine package manager and install dependencies
    if [[ -f "package-lock.json" ]]; then
        echo "Using npm"
        npm ci
    elif [[ -f "yarn.lock" ]]; then
        echo "Using yarn"
        yarn install --frozen-lockfile
    elif [[ -f "pnpm-lock.yaml" ]]; then
        echo "Using pnpm"
        pnpm install --frozen-lockfile
    else
        echo "Using npm (no lockfile detected)"
        npm install
    fi
    
    # Run build if defined
    if npm run build --if-present 2>/dev/null; then
        echo "Build completed"
    else
        echo "No build script found, skipping build"
    fi
    
    # Run tests if defined
    if npm test --if-present 2>/dev/null; then
        echo "Tests completed"
    else
        echo "No test script found, skipping tests"
    fi
fi

# .NET setup
if [[ $DOTNET_DETECTED -eq 1 ]]; then
    echo "=== Setting up .NET environment ==="
    
    dotnet restore
    dotnet build
    
    # Run tests (skip gracefully if no tests)
    if dotnet test --list-tests &>/dev/null; then
        dotnet test || echo "Tests failed but continuing"
    else
        echo "No tests found, skipping dotnet test"
    fi
fi

# Java setup
if [[ $JAVA_DETECTED -eq 1 ]]; then
    echo "=== Setting up Java environment ==="
    
    if [[ -f "gradlew" ]]; then
        echo "Using Gradle wrapper"
        ./gradlew clean build test || echo "Gradle build/test failed but continuing"
    elif command -v gradle &> /dev/null; then
        echo "Using Gradle"
        gradle clean build test || echo "Gradle build/test failed but continuing"
    elif command -v mvn &> /dev/null; then
        echo "Using Maven"
        mvn clean compile test || echo "Maven build/test failed but continuing"
    else
        echo "No Java build tool found (gradle/mvn), skipping Java setup"
    fi
fi

# Go setup
if [[ $GO_DETECTED -eq 1 ]]; then
    echo "=== Setting up Go environment ==="
    
    go mod tidy
    go build ./...
    
    # Run tests (skip gracefully if no tests)
    if go list ./... | grep -q .; then
        go test ./... || echo "Go tests failed but continuing"
    else
        echo "No Go tests found, skipping go test"
    fi
fi

# Echo versions of relevant tools
echo "=== Tool versions ==="

if command -v python3 &> /dev/null; then
    echo "Python: $(python3 --version)"
fi

if command -v pip &> /dev/null; then
    echo "pip: $(pip --version)"
fi

if command -v node &> /dev/null; then
    echo "Node.js: $(node --version)"
fi

if command -v npm &> /dev/null; then
    echo "npm: $(npm --version)"
fi

if command -v dotnet &> /dev/null; then
    echo ".NET: $(dotnet --version)"
fi

if command -v java &> /dev/null; then
    echo "Java: $(java -version 2>&1 | head -1)"
fi

if command -v go &> /dev/null; then
    echo "Go: $(go version)"
fi

echo "JULES_OK"
exit 0