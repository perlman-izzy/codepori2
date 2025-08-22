#!/usr/bin/env bash
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1

# Jules setup script for multi-stack project detection and setup
# Runs in fresh Ubuntu VM with no state carryover

echo "=== Jules Setup Script Starting ==="

# Install minimal system dependencies
echo "Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq \
    build-essential \
    python3-dev \
    libssl-dev \
    libffi-dev \
    pkg-config \
    git \
    ca-certificates

# Stack detection functions
detect_python() {
    [[ -f "requirements.txt" ]] || [[ -f "pyproject.toml" ]] || [[ $(find . -maxdepth 2 -name "*.py" | wc -l) -gt 0 ]]
}

detect_nodejs() {
    [[ -f "package.json" ]]
}

detect_dotnet() {
    [[ $(find . -maxdepth 2 -name "*.csproj" -o -name "*.sln" | wc -l) -gt 0 ]]
}

detect_java() {
    [[ -f "gradlew" ]] || [[ -f "build.gradle" ]] || [[ -f "pom.xml" ]]
}

detect_go() {
    [[ -f "go.mod" ]]
}

# Python setup function
setup_python() {
    echo "=== Setting up Python environment ==="
    
    # Install python3-pip if not available
    if ! command -v pip3 &> /dev/null; then
        apt-get install -y -qq python3-pip
    fi
    
    # Try to use uv if available, otherwise fall back to venv
    if command -v uv &> /dev/null; then
        echo "Creating virtual environment with uv..."
        uv venv venv
        source venv/bin/activate
        
        # Install dependencies
        if [[ -f "requirements.txt" ]]; then
            echo "Installing dependencies from requirements.txt..."
            uv pip install -r requirements.txt
        elif [[ -f "pyproject.toml" ]]; then
            echo "Installing dependencies from pyproject.toml..."
            uv pip install .
        else
            echo "No requirements.txt or pyproject.toml found, installing pytest only..."
            uv pip install pytest
        fi
    else
        echo "Creating virtual environment with venv..."
        python3 -m venv venv
        source venv/bin/activate
        
        # Upgrade pip in virtual environment
        python -m pip install --upgrade pip
        
        # Install dependencies
        if [[ -f "requirements.txt" ]]; then
            echo "Installing dependencies from requirements.txt..."
            pip install -r requirements.txt
        elif [[ -f "pyproject.toml" ]]; then
            echo "Installing dependencies from pyproject.toml..."
            pip install .
        else
            echo "No requirements.txt or pyproject.toml found, installing pytest only..."
            pip install pytest
        fi
    fi
    
    # Detect additional dependencies from Python files and install them
    echo "Scanning Python files for import dependencies..."
    if grep -r "import requests\|from requests" . --include="*.py" >/dev/null 2>&1; then
        echo "Detected requests dependency, installing..."
        if command -v uv &> /dev/null; then
            uv pip install requests
        else
            pip install requests
        fi
    fi
    
    # Compile all Python files
    echo "Compiling Python files..."
    python -m compileall . -q || echo "Warning: Some Python files failed to compile"
    
    # Run pytest if tests exist
    if [[ -n "$(find . -name "*test*.py" -o -name "test_*.py" -print -quit)" ]] || [[ -d "tests" ]] || [[ -d "test" ]]; then
        echo "Running pytest..."
        python -m pytest -v || echo "Warning: Some tests failed"
    else
        echo "No test files found, skipping pytest"
    fi
}

# Node.js setup function
setup_nodejs() {
    echo "=== Setting up Node.js environment ==="
    
    # Install Node.js and npm if not available
    if ! command -v node &> /dev/null; then
        apt-get install -y -qq nodejs npm
    fi
    
    # Determine package manager and install dependencies
    if [[ -f "yarn.lock" ]] && command -v yarn &> /dev/null; then
        echo "Installing dependencies with yarn..."
        yarn install --frozen-lockfile
    elif [[ -f "pnpm-lock.yaml" ]] && command -v pnpm &> /dev/null; then
        echo "Installing dependencies with pnpm..."
        pnpm install --frozen-lockfile
    else
        echo "Installing dependencies with npm..."
        npm ci || npm install
    fi
    
    # Run build if script exists
    if npm run | grep -q "build"; then
        echo "Running npm run build..."
        npm run build
    fi
    
    # Run tests if script exists
    if npm run | grep -q "test"; then
        echo "Running npm test..."
        npm test || echo "Warning: Some tests failed"
    else
        echo "No test script found, skipping tests"
    fi
}

# .NET setup function
setup_dotnet() {
    echo "=== Setting up .NET environment ==="
    
    # Install .NET SDK if not available
    if ! command -v dotnet &> /dev/null; then
        # Add Microsoft package repository
        wget https://packages.microsoft.com/config/ubuntu/$(lsb_release -rs)/packages-microsoft-prod.deb -O packages-microsoft-prod.deb
        dpkg -i packages-microsoft-prod.deb
        rm packages-microsoft-prod.deb
        apt-get update -qq
        apt-get install -y -qq dotnet-sdk-8.0
    fi
    
    # Restore dependencies
    echo "Restoring .NET dependencies..."
    dotnet restore
    
    # Build project
    echo "Building .NET project..."
    dotnet build --no-restore
    
    # Run tests if they exist
    if [[ -n "$(find . -name "*Test*.csproj" -o -name "*test*.csproj" -print -quit)" ]]; then
        echo "Running .NET tests..."
        dotnet test --no-build --verbosity normal || echo "Warning: Some tests failed"
    else
        echo "No test projects found, skipping tests"
    fi
}

# Java setup function
setup_java() {
    echo "=== Setting up Java environment ==="
    
    # Install Java and Maven if not available
    if ! command -v java &> /dev/null; then
        apt-get install -y -qq default-jdk
    fi
    
    if [[ -f "gradlew" ]]; then
        echo "Using Gradle wrapper..."
        chmod +x gradlew
        ./gradlew clean build
        ./gradlew test || echo "Warning: Some tests failed"
    elif command -v gradle &> /dev/null || apt-get install -y -qq gradle; then
        echo "Using Gradle..."
        gradle clean build
        gradle test || echo "Warning: Some tests failed"
    elif [[ -f "pom.xml" ]]; then
        if ! command -v mvn &> /dev/null; then
            apt-get install -y -qq maven
        fi
        echo "Using Maven..."
        mvn clean compile
        mvn test || echo "Warning: Some tests failed"
    fi
}

# Go setup function
setup_go() {
    echo "=== Setting up Go environment ==="
    
    # Install Go if not available
    if ! command -v go &> /dev/null; then
        apt-get install -y -qq golang-go
    fi
    
    # Tidy dependencies
    echo "Running go mod tidy..."
    go mod tidy
    
    # Build all packages
    echo "Building Go packages..."
    go build ./...
    
    # Run tests if they exist
    if [[ -n "$(find . -name "*_test.go" -print -quit)" ]]; then
        echo "Running Go tests..."
        go test ./... || echo "Warning: Some tests failed"
    else
        echo "No test files found, skipping tests"
    fi
}

# Detect and setup project stack
echo "=== Detecting project stack ==="

PYTHON_DETECTED=false
NODEJS_DETECTED=false
DOTNET_DETECTED=false
JAVA_DETECTED=false
GO_DETECTED=false

if detect_python; then
    echo "Python stack detected"
    PYTHON_DETECTED=true
fi

if detect_nodejs; then
    echo "Node.js stack detected"
    NODEJS_DETECTED=true
fi

if detect_dotnet; then
    echo ".NET stack detected"
    DOTNET_DETECTED=true
fi

if detect_java; then
    echo "Java stack detected"
    JAVA_DETECTED=true
fi

if detect_go; then
    echo "Go stack detected"
    GO_DETECTED=true
fi

# Setup detected stacks
if [[ "$PYTHON_DETECTED" == "true" ]]; then
    setup_python
fi

if [[ "$NODEJS_DETECTED" == "true" ]]; then
    setup_nodejs
fi

if [[ "$DOTNET_DETECTED" == "true" ]]; then
    setup_dotnet
fi

if [[ "$JAVA_DETECTED" == "true" ]]; then
    setup_java
fi

if [[ "$GO_DETECTED" == "true" ]]; then
    setup_go
fi

# Echo versions of relevant tools
echo "=== Tool Versions ==="
if command -v python3 &> /dev/null; then
    echo "Python: $(python3 --version)"
fi
if command -v pip3 &> /dev/null; then
    echo "pip: $(pip3 --version | head -n1)"
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
    echo "Java: $(java -version 2>&1 | head -n1)"
fi
if command -v go &> /dev/null; then
    echo "Go: $(go version)"
fi

echo "=== Setup completed successfully ==="
echo "JULES_OK"
exit 0