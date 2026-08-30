"""Allow ``python -m icourse_blog_index`` to invoke the command line tool."""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
