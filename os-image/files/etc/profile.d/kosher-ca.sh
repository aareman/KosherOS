# KosherOS: developer tools trust the filter's certificate.
#
# In "Filtered internet" mode this computer reads the account's HTTPS with
# its own certificate authority, which is in the system trust store. Browsers,
# curl, git and Go read that store; the language package managers mostly
# carry their own certificate bundles and would refuse every download with a
# certificate error. These variables point each of them at the system bundle
# (which contains the KosherOS authority once inspection is set up, and is
# just Fedora's ordinary bundle otherwise, so they are harmless on an
# account that is not inspected). Kept in step with
# /etc/environment.d/50-kosher-ca.conf, which covers apps started from the
# desktop rather than a shell.
KOSHER_CA_BUNDLE=/etc/pki/tls/certs/ca-bundle.crt
export SSL_CERT_FILE="$KOSHER_CA_BUNDLE"          # Python (ssl), Ruby/gem, uv, httpx, many others
export REQUESTS_CA_BUNDLE="$KOSHER_CA_BUNDLE"     # Python requests
export PIP_CERT="$KOSHER_CA_BUNDLE"               # pip
export NODE_EXTRA_CA_CERTS="$KOSHER_CA_BUNDLE"    # node, npm, npx, yarn, pnpm, bun
export CARGO_HTTP_CAINFO="$KOSHER_CA_BUNDLE"      # cargo
export DENO_TLS_CA_STORE=system                   # deno: use the system store
export UV_NATIVE_TLS=1                            # uv: use the system store
export GIT_SSL_CAINFO="$KOSHER_CA_BUNDLE"         # git, when built against a bundled OpenSSL
unset KOSHER_CA_BUNDLE
