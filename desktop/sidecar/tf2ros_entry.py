import sys

from iac_code.skills.bundled.iac_aliyun.scripts.tf2ros import convert

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: iac-code-tf2ros <terraform_dir> <output_file>", file=sys.stderr)
        raise SystemExit(1)
    convert(sys.argv[1], sys.argv[2])
