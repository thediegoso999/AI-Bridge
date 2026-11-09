# AI-Bridge development instructions

- Use Python 3.11 and the standard library unless a concrete requirement justifies a dependency.
- Keep modules small and separate domain behavior from filesystem, GitHub, and future AI-provider adapters.
- Keep local inspection read-only. Never add implicit network calls or execute workflows as part of inspection.
- Do not assume GitHub Copilot Free exposes an inference API. Any future model integration must use an official provider API and explicit configuration.
- Never commit credentials, request personal access tokens in code, or broaden GitHub permissions without a documented need.
- Add or update `unittest` coverage for behavior changes. CI must fail when no tests are discovered.
- Run `python -m unittest discover -s tests -v` and `python -m aibridge inspect .` before proposing changes.
- Preserve the existing least-privilege GitHub Actions permissions and avoid unrelated changes.