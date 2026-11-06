# shellcheck shell=sh
# (sourced by every login shell, and by Fedora's bashrc for the others.)
# KosherOS: devenv's local proxy on ports an account may bind. Its default
# is 127.0.0.1:80/443, and a port below 1024 makes devenv ask for sudo,
# which nobody has here. Each account gets its own pair, the one the
# firewall keeps for it alone (kosherd/devenvproxy.py: 7100 + 2 * (uid % 400),
# and the next). A value already set wins.
if [ -n "${HOME:-}" ]; then
    kosher_devenv_uid="$(id -u 2>/dev/null || echo 0)"
    kosher_devenv_port=$((7100 + 2 * (kosher_devenv_uid % 400)))
    : "${DEVENV_PROXY_LISTEN:=127.0.0.1:$kosher_devenv_port}"
    : "${DEVENV_PROXY_HTTPS_LISTEN:=127.0.0.1:$((kosher_devenv_port + 1))}"
    export DEVENV_PROXY_LISTEN DEVENV_PROXY_HTTPS_LISTEN
    unset kosher_devenv_uid kosher_devenv_port
fi
