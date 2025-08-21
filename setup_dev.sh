#!/bin/bash
# Quick setup script for CodePori development environment

set -e

echo "=== CodePori Development Environment Setup ==="
echo

# Check Python version
python_version=$(python --version)
echo "✅ Found: $python_version"

# Install basic dependencies if needed
echo "📦 Checking dependencies..."

# Check if requests is available
python -c "import requests" 2>/dev/null && echo "✅ requests already available" || {
    echo "Installing requests..."
    pip install requests
}

# Optional: Install pytest for testing
python -c "import pytest" 2>/dev/null && echo "✅ pytest already available" || {
    echo "📦 Installing pytest (optional)..."
    pip install pytest || echo "⚠️  pytest install failed (optional)"
}

# Optional: Install linting tools
python -c "import flake8" 2>/dev/null && echo "✅ flake8 already available" || {
    echo "📦 Installing flake8 (optional)..."  
    pip install flake8 || echo "⚠️  flake8 install failed (optional)"
}

echo
echo "🧪 Running validation..."
python validate_setup.py

echo
echo "🎉 Setup complete!"
echo
echo "💡 Usage:"
echo "  - Basic smoke test: python -c \"import sys; sys.path.insert(0, 'CodePori'); import main; print('SMOKE')\""
echo "  - Full pipeline (needs proxy): cd CodePori && python main.py"
echo "  - Start proxy server: python gemini-flask-57.py"
echo "  - Test runner only: cd CodePori && python testonly.py"
echo
echo "📝 See PROJECT_SETUP_TEMPLATE.md for complete configuration details."