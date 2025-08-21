# CodePori Project Setup Template

PROJECT NAME: CodePori - AI Code Generation Pipeline  
REPO: https://github.com/perlman-izzy/codepori2 (public)

## LANGUAGES/TOOLS (check all that apply):
- [x] Python  (version: 3.12.3)
- [ ] Node/PNPM/NPM (node version: <e.g., 20>)
- [ ] Java/Gradle (jdk: <e.g., 21>)
- [ ] .NET (sdk: <e.g., 8.0>)
- [ ] Android SDK (yes/no)
- [ ] Other: <list>

## PYTHON PACKAGING (pick one):
- [ ] requirements.txt
- [ ] pyproject.toml (backend: <e.g., hatch/poetry/setuptools>)
- [x] none (infer packages from imports)

## TEST COMMANDS (write exact commands; I'll wire them):
- Unit tests: `python -m pytest CodePori/output/code/tests/ -v`
- Lint (optional): `python -m flake8 CodePori/main.py --max-line-length=120 --count`  
- Build (optional): `python -c "import ast; ast.parse(open('CodePori/main.py').read()); print('Build OK')"`

## ENTRY/SMOKE (short, non-blocking check):
- Python: `python -c "import sys; sys.path.insert(0, 'CodePori'); import main; print('SMOKE')"`

## SYSTEM PACKAGES NEEDED (apt): 
None required for basic functionality. Optional:
- build-essential (for pip packages with C extensions)
- python3-dev (for development headers)

## NATIVE/LIB EXTRAS:
- TA-Lib needed? no
- OpenCV? no (unless facial recognition project is generated)
- FFMPEG? no 
- CUDA/GPU? no (Jules VMs are CPU-only—confirmed)

## EXTERNAL SERVICES NEEDED AT TEST TIME (prefer NONE):
- Gemini Flask Proxy at http://localhost:8000 (for full functionality)
- Note: Can run in offline mode for syntax/import testing

## ENV VARS REQUIRED TO IMPORT/RUN TESTS (safe dummy values ok):
- GEMINI_PROXY_BASE=http://localhost:8000 (proxy endpoint)
- CP_HTTP_TIMEOUT=180 (HTTP timeout in seconds)
- CP_ENABLE_LINTER=1 (enable linting)
- CP_LINT_REPROMPTS=2 (max lint reprompts)
- CP_JSON_RETRIES=6 (JSON parsing retries)
- CP_REPAIR_RETRIES=6 (repair attempt retries)
- CP_AUTOFIX_RELATIVE=1 (autofix relative imports)
- CP_STRICT_CODEPORI=1 (strict mode)

## TIME/LONG-RUNNING GUARDRAILS:
- Max setup time: 5 min
- Do NOT start servers, websockets, infinite loops (agree: yes)
- Note: Main pipeline can take 15+ minutes when Gemini proxy is available

## SPECIAL NOTES:
- This is an AI-powered code generation pipeline that uses Gemini models
- Requires a Flask proxy server (gemini-flask-57.py) for full functionality
- Can be tested for imports/syntax without the proxy server
- Generates code in CodePori/output/code/ directory
- Has multiple main.py variants for different features
- Uses bot configuration files (*.txt) for LLM prompts
- Safe to run without external services for basic validation
- Project generates Python applications based on text descriptions
- Example project: Facial recognition attendance system (see project_description.txt)