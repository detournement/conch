#!/usr/bin/env python3
"""Build (and optionally sign) the reproducible conch worker artifact.

Usage:
    python tools/build_worker_artifact.py --out dist/conch-worker.pyz \
        [--sign-key ~/.config/conch/fleet/signing-key] \
        [--principal fleet@example.org --emit-allowed-signers]

The human-readable build summary goes to stderr so that stdout carries
only machine output: with --emit-allowed-signers, redirecting stdout
(`> allowed_signers`) yields exactly the one signer line and nothing
else. --allowed-signers-out PATH writes the trust anchor to a file
directly. Signing uses OpenSSH sshsig (ssh-keygen -Y sign);
verification on hosts is mandatory and fail-closed — see
conch/fleet/artifacts.py.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conch.fleet.artifacts import (  # noqa: E402
    ArtifactError,
    allowed_signers_line,
    build_worker_artifact,
    sign_manifest,
    write_manifest,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the signed single-file conch worker artifact."
    )
    parser.add_argument(
        "--out", required=True, metavar="PATH",
        help="Output .pyz path (manifest written alongside).",
    )
    parser.add_argument(
        "--packages", default="conch,pygments", metavar="P1,P2",
        help="Comma-separated packages to bundle (default: conch,pygments).",
    )
    parser.add_argument(
        "--sign-key", default="", metavar="KEY",
        help="OpenSSH private key: sign the manifest with ssh-keygen -Y.",
    )
    parser.add_argument(
        "--principal", default="", metavar="NAME",
        help="Principal for --emit-allowed-signers.",
    )
    parser.add_argument(
        "--emit-allowed-signers", action="store_true",
        help="Print the allowed-signers line for --sign-key's public key"
             " to stdout (the only stdout output, so redirection yields a"
             " valid allowed_signers file).",
    )
    parser.add_argument(
        "--allowed-signers-out", default="", metavar="PATH",
        help="Write the allowed-signers line for --sign-key's public key"
             " to PATH.",
    )
    args = parser.parse_args(argv)
    out = Path(args.out)
    packages = tuple(
        name.strip() for name in args.packages.split(",") if name.strip()
    )
    emit = sys.stderr  # summary is human-readable; stdout is machine output
    try:
        manifest = build_worker_artifact(out, packages=packages)
        manifest_path = write_manifest(out, manifest)
        print(f"artifact: {out}", file=emit)
        print(f"sha256:   {manifest['artifact']['sha256']}", file=emit)
        print(f"size:     {manifest['artifact']['size']}", file=emit)
        print(f"manifest: {manifest_path}", file=emit)
        if args.sign_key:
            sig = sign_manifest(manifest_path, args.sign_key)
            print(f"signature: {sig}", file=emit)
            if args.emit_allowed_signers or args.allowed_signers_out:
                if not args.principal:
                    parser.error(
                        "--emit-allowed-signers/--allowed-signers-out"
                        " need --principal"
                    )
                pub = Path(args.sign_key + ".pub")
                signer_line = allowed_signers_line(args.principal, pub)
                if args.emit_allowed_signers:
                    sys.stdout.write(signer_line)
                if args.allowed_signers_out:
                    anchor = Path(args.allowed_signers_out)
                    anchor.parent.mkdir(parents=True, exist_ok=True)
                    anchor.write_text(signer_line, encoding="utf-8")
                    print(f"allowed_signers: {anchor}", file=emit)
        elif args.emit_allowed_signers or args.allowed_signers_out:
            parser.error(
                "--emit-allowed-signers/--allowed-signers-out need"
                " --sign-key"
            )
    except ArtifactError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
