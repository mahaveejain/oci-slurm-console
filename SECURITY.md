# Security

This console is intended for trusted administrators. It does not provide built-in user authentication or per-user authorization. Bind it to loopback and use an authenticated reverse proxy or SSH tunnel.

Review all optional lifecycle helpers and sudoers rules before installation. Drain, resume, reboot, and workload submission can affect running jobs and infrastructure. Enable only the capabilities needed by your deployment.

Do not disclose vulnerabilities or credentials in public issues. Use GitHub private vulnerability reporting if enabled by the maintainer; otherwise contact the maintainer privately through their GitHub profile before sharing details.

No production support or security maintenance commitment is currently defined.
