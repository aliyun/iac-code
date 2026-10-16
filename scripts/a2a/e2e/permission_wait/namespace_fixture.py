"""Run the real permission HTTP fixture as PID 1 in an isolated PID domain."""

import argparse

from tests.a2a.test_permission_prompt_e2e import _serve


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--config-dir", required=True)
    parser.add_argument("--shared-dir", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    _serve(
        args.root,
        args.port,
        config_dir=args.config_dir,
        persistence_dir=args.shared_dir + "/a2a",
        backup_dir=args.shared_dir + "/backup",
        observation_dir=args.shared_dir,
        resident_timeout=0.2,
        tool_count=1,
        host=args.host,
    )


if __name__ == "__main__":
    main()
