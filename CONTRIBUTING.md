# Contributing to HERA Multiagent

Thank you for your interest in contributing to **HERA Multiagent**! We welcome bug reports, feature requests, adapter extensions, and documentation improvements.

---

## 🚀 How to Contribute

1. **Fork the Repository:** Create your own fork of the repo on GitHub.
2. **Clone and Branch:**
   ```bash
   git clone https://github.com/Kaio-Comk/hera-multiagent.git
   cd hera-multiagent
   git checkout -b feat/my-new-feature
   ```
3. **Set Up Development Environment:**
   ```bash
   python -m venv .venv
   .venv\Scripts\activate  # Windows
   pip install -e .
   pip install pytest pytest-asyncio httpx pydantic pyyaml
   ```
4. **Run the Test Suite:**
   All contributions must pass all 64 automated tests:
   ```bash
   python -m pytest tests -v
   ```
5. **Security and Hardening Guidelines:**
   - Any new validation commands must use structured `argv` execution (`create_subprocess_exec` with `shell=False`).
   - Any remote or cloud communications must pass through the `PrivacyGuard` DLP gate.
   - Do not commit SQLite database files, `.env` files, or temporary worktrees.
6. **Open a Pull Request:** Submit your PR with a clear description and test evidence.

---

## 📜 Code of Conduct

Please maintain a collaborative, respectful, and constructive environment for all developers and AI pair programmers.
