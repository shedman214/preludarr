#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Print the third-party package list (docker/THIRD_PARTY_PACKAGES.txt) from a built Preludarr image.
# Usage: tools/third_party_list.sh IMAGE > docker/THIRD_PARTY_PACKAGES.txt
set -eu
IMAGE=${1:?usage: tools/third_party_list.sh IMAGE}
docker run --rm --network none --entrypoint sh "$IMAGE" -c '
echo "# Third-party packages in the Preludarr Docker image (generated from the image; name-version arch {source package} (licence))"
echo "# Alpine Linux $(cat /etc/alpine-release) packages:"
apk list -I 2>/dev/null | sed "s/ \[installed\]$//" | sort
echo "# Python packages (name version | licence):"
python3 - <<EOF
import importlib.metadata as m
rows = {}
for d in m.distributions():
    md = d.metadata
    name = md["Name"]
    lic = md.get("License-Expression") or ""
    if not lic:
        cls = [c.split("::")[-1].strip().replace(" License", "") for c in md.get_all("Classifier") or [] if c.startswith("License ::")]
        lic = ", ".join(cls) or ((md.get("License") or "unknown").splitlines() or ["unknown"])[0][:60]
    lic = {"Mozilla Public 2.0 (MPL 2.0)": "MPL-2.0", "Apache Software": "Apache-2.0"}.get(lic, lic)  # classifier names -> SPDX
    rows[name.lower()] = "%s %s | %s" % (name, d.version, lic)
for k in sorted(rows):
    print(rows[k])
EOF
'
