"""Scan engine orchestration framework.

Generic infrastructure only — no concrete scanner (ZAP, Nuclei, Trivy,
Semgrep, Gitleaks, Nmap, Nikto, or otherwise) is implemented here or
anywhere in this codebase yet. `app.scan_engine.registry.ScannerRegistry`
is the extension point a future scanner plugs into; see
docs/scanner-interface.md.
"""
