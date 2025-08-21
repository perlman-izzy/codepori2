#!/usr/bin/env python3
"""
Quick setup validation script for CodePori project
"""
import sys
import os
import subprocess

def test_python_version():
    """Test Python version compatibility"""
    if sys.version_info < (3, 8):
        print(f"❌ Python {sys.version} is too old. Need 3.8+")
        return False
    print(f"✅ Python {sys.version} OK")
    return True

def test_imports():
    """Test core imports"""
    try:
        import ast
        import importlib
        import json
        import os
        import pathlib
        import re
        import shutil
        import subprocess
        import sys
        import textwrap
        import time
        import traceback
        from dataclasses import dataclass
        from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
        import requests
        print("✅ Core imports OK")
        return True
    except ImportError as e:
        print(f"❌ Import error: {e}")
        return False

def test_main_import():
    """Test main module import"""
    try:
        sys.path.insert(0, 'CodePori')
        import main
        print("✅ Main module import OK")
        return True
    except Exception as e:
        print(f"❌ Main import error: {e}")
        return False

def test_file_structure():
    """Test expected file structure"""
    required_files = [
        'CodePori/main.py',
        'CodePori/project_description.txt',
        'CodePori/manager_bot.txt',
        'gemini-flask-57.py'
    ]
    
    all_good = True
    for file in required_files:
        if os.path.exists(file):
            print(f"✅ {file} found")
        else:
            print(f"❌ {file} missing")
            all_good = False
    
    return all_good

def main():
    print("=== CodePori Project Setup Validation ===\n")
    
    tests = [
        test_python_version,
        test_imports,
        test_file_structure,
        test_main_import,
    ]
    
    results = []
    for test in tests:
        results.append(test())
        print()
    
    if all(results):
        print("🎉 All validation checks passed!")
        print("Project is ready for development.")
        return 0
    else:
        print("❌ Some validation checks failed.")
        print("Please fix the issues above.")
        return 1

if __name__ == "__main__":
    sys.exit(main())