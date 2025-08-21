# CodePori Project Documentation

## Overview
CodePori is an AI-powered code generation pipeline that uses Google Gemini models to generate complete Python applications from text descriptions. It features a multi-stage pipeline with planning, code generation, linting, testing, and repair capabilities.

## Project Structure
```
codepori2/
├── CodePori/                    # Main application directory
│   ├── main.py                  # Primary pipeline script
│   ├── testonly.py             # Test runner for generated code
│   ├── project_description.txt  # Project requirements input
│   ├── manager_bot.txt          # Planning bot prompt
│   ├── dev_1.txt               # Developer bot prompt 1
│   ├── dev_2.txt               # Developer bot prompt 2
│   ├── finalizer_bot_1.txt     # Finalizer bot prompt 1
│   ├── finalizer_bot_2.txt     # Finalizer bot prompt 2
│   ├── verfication_bot.txt     # Verification bot prompt
│   └── output/                 # Generated code output
│       ├── code/              # Generated Python project
│       └── run.log            # Pipeline execution log
├── gemini-flask-57.py          # Flask proxy server for Gemini API
├── validate_setup.py           # Setup validation script
├── setup_dev.sh               # Development environment setup
└── PROJECT_SETUP_TEMPLATE.md  # Project configuration template
```

## Quick Start

### 1. Basic Setup Validation
```bash
python validate_setup.py
```

### 2. Install Dependencies (Optional)
```bash
./setup_dev.sh
```

### 3. Smoke Test
```bash
python -c "import sys; sys.path.insert(0, 'CodePori'); import main; print('SMOKE')"
```

## Pipeline Commands

### Basic Code Generation (needs proxy server)
```bash
cd CodePori
python main.py
```

### Test Runner Only (for generated code)
```bash
cd CodePori  
python testonly.py
```

### Start Gemini Proxy Server
```bash
python gemini-flask-57.py
```

## Testing Commands

### Lint Generated Code
```bash
python -m flake8 CodePori/ --max-line-length=120
```

### Run Tests (if generated)
```bash
python -m pytest CodePori/output/code/tests/ -v
```

### Syntax Check
```bash
python -c "import CodePori.main; print('Syntax OK')"
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GEMINI_PROXY_BASE` | `http://localhost:8000` | Gemini proxy server URL |
| `CP_HTTP_TIMEOUT` | `180` | HTTP request timeout (seconds) |
| `CP_ENABLE_LINTER` | `1` | Enable linting (1=on, 0=off) |
| `CP_LINT_REPROMPTS` | `2` | Maximum lint repair attempts |
| `CP_JSON_RETRIES` | `6` | JSON parsing retry attempts |
| `CP_REPAIR_RETRIES` | `6` | Code repair retry attempts |
| `CP_AUTOFIX_RELATIVE` | `1` | Auto-fix relative imports |
| `CP_STRICT_CODEPORI` | `1` | Enable strict mode |

## Example Usage

### 1. Generate a Python Application
1. Edit `CodePori/project_description.txt` with your requirements
2. Start the proxy server: `python gemini-flask-57.py`
3. Run the pipeline: `cd CodePori && python main.py`
4. Check generated code in: `CodePori/output/code/`

### 2. Test Generated Code
```bash
cd CodePori
python testonly.py
```

### 3. Manual Testing
```bash
cd CodePori/output/code
python -m pytest tests/ -v
```

## Pipeline Stages

1. **Planning** - Analyzes requirements and creates project structure
2. **Generation** - Generates Python files and tests
3. **Syntax Gate** - Validates Python syntax
4. **Package Normalization** - Fixes imports and package structure  
5. **Linting** - Code quality checks with targeted repairs
6. **Testing** - Runs pytest with plugin awareness
7. **Repair** - Iterative fixes based on test failures

## Dependencies

### Required
- Python 3.8+ (tested with 3.12.3)
- requests library

### Optional
- pytest (for testing)
- flake8 (for linting)

### External Services
- Gemini API proxy server (for full functionality)
- Can run offline for syntax/import validation

## Limitations

- Requires Gemini proxy server for AI functionality
- Generated code quality depends on prompt engineering
- Max processing time can be 15+ minutes for complex projects
- GPU acceleration not supported (CPU-only)

## Troubleshooting

### Connection Refused Error
- Ensure Gemini proxy server is running: `python gemini-flask-57.py`
- Check `GEMINI_PROXY_BASE` environment variable

### Import Errors
- Run smoke test: `python validate_setup.py`
- Check Python path and module imports

### Generated Code Issues
- Use test runner: `python testonly.py` 
- Check logs in: `CodePori/output/run.log`
- Adjust retry limits via environment variables

## Example Project
The repository includes a sample project description for a facial recognition attendance system demonstrating the expected input format and capabilities.