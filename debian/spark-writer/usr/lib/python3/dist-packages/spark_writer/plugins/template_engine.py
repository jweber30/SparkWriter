"""Dependency-free template rendering for SparkPlug manifests.

Supports simple variable substitution and truthy conditional blocks while
preserving compatibility with hyphenated field identifiers used by manifest
config IDs.
"""

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union


@dataclass(frozen=True)
class _TextNode:
    value: str


@dataclass(frozen=True)
class _VarNode:
    name: str


@dataclass(frozen=True)
class _IfNode:
    name: str
    true_branch: List["_Node"]
    false_branch: List["_Node"]


_Node = Union[_TextNode, _VarNode, _IfNode]


class SparkTemplateEngine:
    """Small renderer for user-controlled template content.

    Design philosophy:
    - Users provide complete scripts/content
    - Variables are their own data (authkeys, hostnames, etc.)
    - No shell escaping needed - user controls the context
    - Fails fast on undefined variables
    """

    _TOKEN_PATTERN = re.compile(r"(\{\{.*?\}\}|\{%.*?%\})", re.DOTALL)
    _NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")

    def validate_template(self, template_string: str) -> bool:
        """Validate template syntax."""
        try:
            self._parse(template_string)
            return True
        except ValueError:
            return False

    def _build_aliases(self, values: Dict[str, Any]) -> Dict[str, str]:
        """Map hyphenated keys to underscore aliases."""
        aliases: Dict[str, str] = {}
        for key in values:
            if '-' in key:
                aliases[key] = key.replace('-', '_')
        return aliases

    def _validate_name(self, name: str) -> str:
        normalized = name.strip()
        if not normalized or not self._NAME_PATTERN.match(normalized):
            raise ValueError(f"Invalid template identifier: '{name}'")
        return normalized

    def _parse_tag(self, token: str) -> Tuple[str, str]:
        if token.startswith("{{"):
            return ("var", self._validate_name(token[2:-2]))

        tag = token[2:-2].strip()
        if tag == "else":
            return ("else", "")
        if tag == "endif":
            return ("endif", "")
        if tag.startswith("if "):
            return ("if", self._validate_name(tag[3:]))
        raise ValueError(f"Unsupported template tag: '{tag}'")

    def _parse(
        self,
        template_string: str,
        start_pos: int = 0,
        stop_tags: Optional[set[str]] = None,
    ) -> Tuple[List[_Node], int, Optional[str]]:
        nodes: List[_Node] = []
        pos = start_pos
        stop_tags = stop_tags or set()

        while True:
            match = self._TOKEN_PATTERN.search(template_string, pos)
            if not match:
                if pos < len(template_string):
                    nodes.append(_TextNode(template_string[pos:]))
                return nodes, len(template_string), None

            if match.start() > pos:
                nodes.append(_TextNode(template_string[pos:match.start()]))

            tag_type, tag_value = self._parse_tag(match.group(0))
            pos = match.end()

            if tag_type in stop_tags:
                return nodes, pos, tag_type
            if tag_type in {"else", "endif"}:
                raise ValueError(f"Unexpected template tag: '{tag_type}'")
            if tag_type == "var":
                nodes.append(_VarNode(tag_value))
                continue

            true_branch, pos, stop_tag = self._parse(
                template_string,
                pos,
                {"else", "endif"},
            )
            false_branch: List[_Node] = []

            if stop_tag == "else":
                false_branch, pos, stop_tag = self._parse(
                    template_string,
                    pos,
                    {"endif"},
                )
            if stop_tag != "endif":
                raise ValueError(f"Missing endif for template if: '{tag_value}'")

            nodes.append(_IfNode(tag_value, true_branch, false_branch))

    def _resolve_name(self, name: str, context: Dict[str, Any]) -> Any:
        if name in context:
            return context[name]
        raise ValueError(f"'{name}' is undefined")

    def _render_nodes(self, nodes: List[_Node], context: Dict[str, Any]) -> str:
        rendered: List[str] = []
        for node in nodes:
            if isinstance(node, _TextNode):
                rendered.append(node.value)
            elif isinstance(node, _VarNode):
                rendered.append(str(self._resolve_name(node.name, context)))
            else:
                branch = node.true_branch if self._resolve_name(node.name, context) else node.false_branch
                rendered.append(self._render_nodes(branch, context))
        return "".join(rendered)

    def render(self, template_string: str, values: Dict[str, Any]) -> str:
        """Render a template with user-provided values.

        Args:
            template_string: User's script/content with {{variable}} placeholders
            values: User-provided configuration values

        Returns:
            String with variables replaced

        Raises:
            ValueError: If template is invalid or references undefined variables
        """
        aliases = self._build_aliases(values)
        context: Dict[str, Any] = dict(values)
        for original, alias in aliases.items():
            context[alias] = values[original]

        nodes, _, stop_tag = self._parse(template_string)
        if stop_tag is not None:
            raise ValueError(f"Unexpected template tag: '{stop_tag}'")
        rendered = self._render_nodes(nodes, context)
        if rendered.endswith("\n"):
            return rendered[:-1]
        return rendered
