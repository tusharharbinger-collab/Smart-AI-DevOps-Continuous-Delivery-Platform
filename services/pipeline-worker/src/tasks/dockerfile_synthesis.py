"""
services/pipeline-worker/src/tasks/dockerfile_synthesis.py

Generates a minimal, standard Dockerfile for a repo that has none, given
what shared/repo_scanner.py's deterministic detection already found (a
recognized language manifest + a real start_command signal). This is the
"synthesized" build method's other half — the scanner decides WHETHER a
language was detected; this decides WHAT Dockerfile that implies. Kept
deliberately separate so the platform's single reliability guarantee ("we
produce a build unless it's a real human-side gap") funnels through the
exact same `docker build` call in build_task.py either way — a synthesized
Dockerfile is written into the cloned workspace and built exactly like a
human-authored one, no separate build code path to keep correct.

Only ever called with a `start_command` that came from a REAL signal
(e.g. package.json's own "scripts.start") or one the human explicitly
typed in the wizard — never a guess. See repo_scanner.py's module
docstring for why that boundary matters. The one exception is a "spa"
framework and the "static" language — neither has a runtime process to
start at all (nginx serves pre-built/static files), so start_command is
never required for them.

Guaranteed Live Web App CI/CD — real gap this closes: the original single
Node.js template ran `npm install --omit=dev`, which broke any Vite/React/
Next.js build — the tools that actually PRODUCE the app (vite, tsc,
next) live in devDependencies, so the build step itself would fail before
ever reaching the app's own code. There was also no path at all for a
plain static site (no package.json, no Dockerfile) or a Next.js app — both
fell through to a human-facing "unsupported" error for something that is
completely buildable. Every generated web-facing template (spa/nextjs/
static) also writes its own SPA-fallback nginx config (`try_files $uri
/index.html`) inline via `RUN printf` — the exact pattern CLAUDE.md/
KNOWLEDGE_BASE.md already document as the one that makes an app tolerate
the shared ALB's un-strippable path prefix, so a synthesized web app
doesn't silently inherit the same 404 trap a hand-written Dockerfile has
to remember to avoid.
"""
import json

# Nginx SPA/static config template and entrypoint script.
# The platform's shared ALB (smartcd-platform-alb) routes projects by path prefix
# (e.g. /api/v1/my-app) and cannot strip prefixes via forward actions.
# When a real PATH_PREFIX is supplied at container startup (e.g. /api/v1/my-app),
# /docker-entrypoint.d/40-path-prefix.sh normalizes it and renders default.conf with:
#   location <prefix>/ { alias /usr/share/nginx/html/; try_files $uri $uri/ /index.html; }
# stripping the prefix from filesystem lookups so assets load correctly.
# If PATH_PREFIX is empty or "/", it renders:
#   location / { try_files $uri $uri/ /index.html; }
# preserving standard root-based access for local/non-ALB environments.
#
# Built with literal `\n` sequences within single-line RUN instructions so the
# Dockerfile parser does not reject split lines, while `printf` interprets them
# as real newlines when the image is built.
_NGINX_TEMPLATE_CONTENT = (
    "server {\\n"
    "    listen 80;\\n"
    "    root /usr/share/nginx/html;\\n"
    "    index index.html;\\n"
    "    location ${PATH_PREFIX} {\\n"
    "        ${NGINX_ALIAS}\\n"
    "        try_files $uri $uri/ /index.html;\\n"
    "    }\\n"
    "}\\n"
)

_ENTRYPOINT_SCRIPT_CONTENT = (
    "#!/bin/sh\\n"
    "set -e\\n"
    "PREFIX=\"${PATH_PREFIX:-}\"\\n"
    "if [ -n \"$PREFIX\" ] && [ \"$PREFIX\" != \"/\" ]; then\\n"
    "    case \"$PREFIX\" in\\n"
    "        /*) ;;\\n"
    "        *) PREFIX=\"/$PREFIX\" ;;\\n"
    "    esac\\n"
    "    case \"$PREFIX\" in\\n"
    "        */) ;;\\n"
    "        *) PREFIX=\"$PREFIX/\" ;;\\n"
    "    esac\\n"
    "    export PATH_PREFIX=\"$PREFIX\"\\n"
    "    export NGINX_ALIAS=\"alias /usr/share/nginx/html/;\"\\n"
    "else\\n"
    "    export PATH_PREFIX=\"/\"\\n"
    "    export NGINX_ALIAS=\"\"\\n"
    "fi\\n"
    "envsubst \"\\$PATH_PREFIX \\$NGINX_ALIAS\" < /etc/nginx/templates/default.conf.template > /etc/nginx/conf.d/default.conf\\n"
)

_NGINX_CONFIG_RUN_LINE = (
    "RUN mkdir -p /etc/nginx/templates /docker-entrypoint.d && "
    f"printf '{_NGINX_TEMPLATE_CONTENT}' > /etc/nginx/templates/default.conf.template && "
    f"printf '{_ENTRYPOINT_SCRIPT_CONTENT}' > /docker-entrypoint.d/40-path-prefix.sh && "
    "chmod +x /docker-entrypoint.d/40-path-prefix.sh"
)

TEMPLATES: dict[str, str] = {
    "python": """\
FROM python:3.11-slim
WORKDIR /app
COPY {manifest_filename} .
RUN pip install --no-cache-dir -r {manifest_filename}
COPY . .
CMD {start_command_json}
""",
    "go": """\
FROM golang:1.22 AS build
WORKDIR /app
COPY . .
RUN go build -o /app/server .

FROM debian:bookworm-slim
WORKDIR /app
COPY --from=build /app/server .
CMD {start_command_json}
""",
}

# Guaranteed Live Web App CI/CD — a Node repo is no longer one template.
# Keyed by shared/repo_scanner.py's BuildDetection.framework
# ("node-server" | "spa" | "nextjs"); "node-server" is today's original
# single-stage template with its one real bug fixed (see module docstring).
NODE_TEMPLATES: dict[str, str] = {
    "node-server": """\
FROM node:20-slim
WORKDIR /app
COPY {manifest_filename} .
RUN npm install
COPY . .
CMD {start_command_json}
""",
    # Vite/CRA: build to static assets, then serve them with nginx — no
    # Node runtime in the final image at all. `mkdir -p /out` + the
    # cp-either-dist-or-build fallback means this doesn't need to guess
    # Vite's `dist/` vs. CRA's `build/` output folder ahead of time; it
    # copies whichever one the build step actually produced.
    # {nginx_config_run_line} is a format ARGUMENT, not pre-embedded text —
    # the nginx config it carries contains literal `{`/`}` braces (a real
    # `server { ... }` block), which str.format() would otherwise try to
    # parse as more format fields if they were part of the template string
    # itself rather than a substituted value.
    "spa": (
        "FROM node:20-slim AS build\n"
        "WORKDIR /app\n"
        "COPY {manifest_filename} .\n"
        "RUN npm install\n"
        "COPY . .\n"
        "RUN npm run build && mkdir -p /out && "
        "(cp -r dist/. /out/ 2>/dev/null || cp -r build/. /out/)\n"
        "\n"
        "FROM nginx:alpine\n"
        "COPY --from=build /out /usr/share/nginx/html\n"
        "{nginx_config_run_line}\n"
        "EXPOSE 80\n"
    ),
    # Next.js still exec's a real Node process (`next start`), unlike a
    # plain SPA — two stages purely to keep devDependencies (the build
    # toolchain) out of the final runtime image.
    "nextjs": """\
FROM node:20-slim AS build
WORKDIR /app
COPY {manifest_filename} .
RUN npm install
COPY . .
RUN npm run build

FROM node:20-slim
WORKDIR /app
COPY --from=build /app .
EXPOSE 3000
CMD {start_command_json}
""",
}

# A static site's Dockerfile needs no manifest/start_command at all — it's
# just files nginx serves. {static_root} is the folder containing the
# detected index.html (see synthesize_dockerfile below), so a site whose
# real content lives in e.g. "public/" copies only that subtree, not the
# whole repo (which may contain unrelated source files, a README, etc.).
STATIC_TEMPLATE = (
    "FROM nginx:alpine\n"
    "COPY {static_root} /usr/share/nginx/html\n"
    "{nginx_config_run_line}\n"
    "EXPOSE 80\n"
)

SUPPORTED_LANGUAGES = frozenset(set(TEMPLATES.keys()) | {"node", "static"})


class UnsupportedLanguageError(Exception):
    pass


def synthesize_dockerfile(
    language: str, manifest_path: str, start_command: str | None, framework: str | None = None
) -> str:
    """
    `manifest_path` may include a subfolder (e.g. "backend/requirements.txt")
    when root_directory scopes the build to that subfolder — COPY paths
    here are relative to the same build context build_task.py already
    resolves, so this doesn't need to know the absolute repo layout.

    `framework` is only ever meaningful when language == "node" (see
    repo_scanner.py's BuildDetection.framework) and defaults to
    "node-server" — today's original single-template behavior — when not
    given, so any existing caller that doesn't pass it is unaffected.
    """
    if language == "static":
        static_root = manifest_path.rsplit("/", 1)[0] if "/" in manifest_path else "."
        return STATIC_TEMPLATE.format(static_root=static_root, nginx_config_run_line=_NGINX_CONFIG_RUN_LINE)

    if language == "node":
        template_key = framework or "node-server"
        if template_key not in NODE_TEMPLATES:
            raise UnsupportedLanguageError(
                f"No synthesized-Dockerfile template for framework={template_key!r}. "
                f"Supported: {sorted(NODE_TEMPLATES.keys())}."
            )
        manifest_filename = manifest_path.rsplit("/", 1)[-1]
        # nginx_config_run_line is harmlessly ignored by the "node-server"/
        # "nextjs" templates, which don't reference that field at all.
        return NODE_TEMPLATES[template_key].format(
            manifest_filename=manifest_filename,
            start_command_json=json.dumps(start_command.split()) if start_command else "[]",
            nginx_config_run_line=_NGINX_CONFIG_RUN_LINE,
        )

    if language not in TEMPLATES:
        raise UnsupportedLanguageError(
            f"No synthesized-Dockerfile template for language={language!r}. "
            f"Supported: {sorted(SUPPORTED_LANGUAGES)}. A real Dockerfile is required for this language."
        )

    manifest_filename = manifest_path.rsplit("/", 1)[-1]
    return TEMPLATES[language].format(
        manifest_filename=manifest_filename,
        start_command_json=json.dumps(start_command.split()),
    )
