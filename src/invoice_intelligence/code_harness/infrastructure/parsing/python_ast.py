"""Python AST adapter with bounded, diagnostic-first parsing."""

import ast
import io
import tokenize
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import PurePosixPath

from ...application.ports.parser import ParseDiagnostic, ParsedArtifact
from ...domain.code_model import ByteRange, CallEdge, CodeSymbol, LineRange
from ...domain.repository import SnapshotFile
from ...domain.versions import ExecutionVersionBinding
from ...domain.errors import HarnessError, HarnessErrorCode
from .grammar_registry import GrammarRegistry


class PythonAstParser:
    """Parse Python source without exposing stdlib AST objects across the Port."""

    language = "python"

    def __init__(self, grammar_registry: GrammarRegistry) -> None:
        self._grammar_registry = grammar_registry

    async def parse(
        self,
        *,
        snapshot_id: str,
        files: tuple[SnapshotFile, ...],
        contents: Mapping[str, bytes],
        versions: ExecutionVersionBinding,
    ) -> tuple[ParsedArtifact, ...]:
        del snapshot_id
        self._grammar_registry.require(self.language)
        results: list[ParsedArtifact] = []
        for file in files:
            if not file.path.endswith(".py"):
                continue
            results.append(self._parse_file(file, contents.get(file.path), versions))
        if not results:
            raise HarnessError(HarnessErrorCode.PARSER_NOT_CONFIGURED, "no Python files found")
        return tuple(results)

    def _parse_file(
        self,
        file: SnapshotFile,
        source: bytes | None,
        versions: ExecutionVersionBinding,
    ) -> ParsedArtifact:
        if source is None:
            return ParsedArtifact(
                path=file.path,
                symbols=(),
                call_edges=(),
                diagnostics=(
                    ParseDiagnostic("snapshot_content_unavailable", "error", 0, file.size_bytes),
                ),
                ast_fingerprint=file.checksum_sha256,
                versions=versions,
            )
        try:
            text = source.decode("utf-8")
            tree = ast.parse(text, filename=file.path)
        except UnicodeDecodeError:
            return ParsedArtifact(
                path=file.path,
                symbols=(),
                call_edges=(),
                diagnostics=(
                    ParseDiagnostic("invalid_utf8", "error", 0, len(source)),
                ),
                ast_fingerprint=file.checksum_sha256,
                versions=versions,
            )
        except SyntaxError as exc:
            line = getattr(exc, "lineno", 1) or 1
            column = getattr(exc, "offset", 0) or 0
            lines = text.splitlines(keepends=True)
            start = _line_column_to_byte(lines, line, max(column - 1, 0))
            end = _line_column_to_byte(lines, line, column)
            return self._parse_tolerant(
                file=file,
                text=text,
                versions=versions,
                diagnostic=ParseDiagnostic(
                    "syntax_error",
                    "error",
                    start,
                    max(start + 1, end),
                ),
            )

        lines = text.splitlines(keepends=True)
        symbols: list[CodeSymbol] = []
        edges: list[CallEdge] = []
        module_id = f"{file.path}:module"
        symbols.append(
            CodeSymbol(
                symbol_id=module_id,
                file_path=file.path,
                language=self.language,
                name=PurePosixPath(file.path).stem,
                symbol_kind="module",
                parent_symbol=None,
                scope="module",
                byte_range=ByteRange(0, len(source)),
                line_range=LineRange(1, max(1, len(lines))),
                signature=None,
                versions=versions,
            )
        )
        self._visit(
            tree,
            file.path,
            lines,
            versions,
            parent_symbol=module_id,
            symbols=symbols,
            edges=edges,
        )
        fingerprint = sha256(ast.dump(tree, annotate_fields=True).encode("utf-8")).hexdigest()
        return ParsedArtifact(
            path=file.path,
            symbols=tuple(symbols),
            call_edges=tuple(edges),
            diagnostics=(),
            ast_fingerprint=fingerprint,
            versions=versions,
        )

    def _parse_tolerant(
        self,
        *,
        file: SnapshotFile,
        text: str,
        versions: ExecutionVersionBinding,
        diagnostic: ParseDiagnostic,
    ) -> ParsedArtifact:
        """Recover bounded declarations and calls without guessing a full AST."""
        lines = text.splitlines(keepends=True)
        tokens: list[tokenize.TokenInfo] = []
        tokenizer_diagnostic: ParseDiagnostic | None = None
        stream = tokenize.generate_tokens(io.StringIO(text).readline)
        try:
            while True:
                tokens.append(next(stream))
        except StopIteration:
            pass
        except (IndentationError, SyntaxError, tokenize.TokenError) as exc:
            line, column = _token_error_position(exc)
            start = _line_column_to_byte(lines, line, column)
            tokenizer_diagnostic = ParseDiagnostic(
                "partial_token_error",
                "warning",
                start,
                max(start + 1, start),
            )

        entries = _tolerant_entries(
            tokens=tokens,
            path=file.path,
            lines=lines,
            source_length=len(text.encode("utf-8")),
            versions=versions,
        )
        module_id = f"{file.path}:module"
        symbols = [
            CodeSymbol(
                symbol_id=module_id,
                file_path=file.path,
                language=self.language,
                name=PurePosixPath(file.path).stem,
                symbol_kind="module",
                parent_symbol=None,
                scope="module",
                byte_range=ByteRange(0, len(text.encode("utf-8"))),
                line_range=LineRange(1, max(1, len(lines))),
                signature=None,
                versions=versions,
            )
        ]
        symbols.extend(entry.symbol for entry in entries)
        edges = _tolerant_call_edges(
            tokens=tokens,
            entries=entries,
            path=file.path,
            lines=lines,
            versions=versions,
        )
        diagnostics = (diagnostic,) + (
            (tokenizer_diagnostic,) if tokenizer_diagnostic is not None else ()
        )
        return ParsedArtifact(
            path=file.path,
            symbols=tuple(symbols),
            call_edges=tuple(edges),
            diagnostics=diagnostics,
            ast_fingerprint=f"partial:{file.checksum_sha256}",
            versions=versions,
            parse_status="partial",
        )

    def _visit(
        self,
        node: ast.AST,
        path: str,
        lines: list[str],
        versions: ExecutionVersionBinding,
        *,
        parent_symbol: str | None,
        symbols: list[CodeSymbol],
        edges: list[CallEdge],
    ) -> None:
        current_symbol = parent_symbol
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            current_symbol = f"{path}:{node.lineno}:{node.name}"
            start, end = _node_bytes(node, lines)
            signature = _signature(node)
            symbols.append(
                CodeSymbol(
                    symbol_id=current_symbol,
                    file_path=path,
                    language=self.language,
                    name=node.name,
                    symbol_kind=kind,
                    parent_symbol=parent_symbol,
                    scope="module" if parent_symbol is None else parent_symbol,
                    byte_range=ByteRange(start, end),
                    line_range=LineRange(node.lineno, getattr(node, "end_lineno", node.lineno)),
                    signature=signature,
                    versions=versions,
                    parameters=_parameters(node),
                )
            )
        if isinstance(node, ast.Call) and current_symbol is not None:
            callee = _call_name(node.func)
            if callee:
                start, end = _node_bytes(node, lines)
                edges.append(
                    CallEdge(
                        caller_symbol_id=current_symbol,
                        callee_name=callee,
                        file_path=path,
                        byte_range=ByteRange(start, end),
                        versions=versions,
                    )
                )
        for child in ast.iter_child_nodes(node):
            self._visit(
                child,
                path,
                lines,
                versions,
                parent_symbol=current_symbol,
                symbols=symbols,
                edges=edges,
            )


def _node_bytes(node: ast.AST, lines: list[str]) -> tuple[int, int]:
    start_line = getattr(node, "lineno", 1)
    end_line = getattr(node, "end_lineno", start_line)
    start_col = getattr(node, "col_offset", 0)
    end_col = getattr(
        node,
        "end_col_offset",
        len(lines[end_line - 1].encode("utf-8").rstrip(b"\r\n")),
    )
    prefix = sum(len(line.encode("utf-8")) for line in lines[: start_line - 1])
    start = prefix + start_col
    end_prefix = sum(len(line.encode("utf-8")) for line in lines[: end_line - 1])
    end = end_prefix + end_col
    return start, max(start, end)


def _line_column_to_byte(lines: list[str], line: int, column: int) -> int:
    if not lines:
        return 0
    index = min(max(line - 1, 0), len(lines) - 1)
    prefix = lines[index][: max(column, 0)]
    return sum(len(item.encode("utf-8")) for item in lines[:index]) + len(
        prefix.encode("utf-8")
    )


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    arguments = [argument.arg for argument in node.args.posonlyargs]
    arguments.extend(argument.arg for argument in node.args.args)
    if node.args.vararg is not None:
        arguments.append(f"*{node.args.vararg.arg}")
    arguments.extend(argument.arg for argument in node.args.kwonlyargs)
    if node.args.kwarg is not None:
        arguments.append(f"**{node.args.kwarg.arg}")
    return f"{node.name}({', '.join(arguments)})"


@dataclass(frozen=True, slots=True)
class _TolerantEntry:
    symbol: CodeSymbol
    start_line: int
    indent_column: int


def _tolerant_entries(
    *,
    tokens: list[tokenize.TokenInfo],
    path: str,
    lines: list[str],
    source_length: int,
    versions: ExecutionVersionBinding,
) -> list[_TolerantEntry]:
    declarations: list[tuple[int, int, str, str, int, str, tuple[str, ...]]] = []
    for index, token in enumerate(tokens):
        if token.type != tokenize.NAME:
            continue
        kind: str | None = None
        keyword_index = index
        if token.string in {"class", "def"}:
            kind = "class" if token.string == "class" else "function"
        elif token.string == "async":
            next_index = _next_significant(tokens, index + 1)
            if next_index is not None and tokens[next_index].string == "def":
                kind = "function"
                keyword_index = next_index
        if kind is None:
            continue
        name_index = _next_significant(tokens, keyword_index + 1)
        if name_index is None or tokens[name_index].type != tokenize.NAME:
            continue
        keyword = tokens[keyword_index]
        name = tokens[name_index].string
        parameters = _tolerant_parameters(tokens, name_index)
        start_line, start_column = keyword.start
        if token.string == "async":
            start_line, start_column = token.start
        declarations.append(
            (
                name_index,
                start_line,
                name,
                kind,
                start_column,
                f"{path}:{start_line}:{name}",
                parameters,
            )
        )

    entries: list[_TolerantEntry] = []
    for declaration_index, (
        name_index,
        start_line,
        name,
        kind,
        indent_column,
        symbol_id,
        parameters,
    ) in enumerate(declarations):
        del declaration_index
        start = _line_column_to_byte(lines, start_line, indent_column)
        end = source_length
        for later in declarations:
            if later[1] > start_line and later[4] <= indent_column:
                end = _line_column_to_byte(lines, later[1], later[4])
                break
        parent = next(
            (
                candidate
                for candidate in reversed(entries)
                if candidate.start_line < start_line
                and candidate.indent_column < indent_column
            ),
            None,
        )
        entries.append(
            _TolerantEntry(
                symbol=CodeSymbol(
                    symbol_id=symbol_id,
                    file_path=path,
                    language="python",
                    name=name,
                    symbol_kind=kind,
                    parent_symbol=parent.symbol.symbol_id if parent else f"{path}:module",
                    scope=parent.symbol.symbol_id if parent else "module",
                    byte_range=ByteRange(start, max(start, end)),
                    line_range=LineRange(start_line, _byte_to_line(lines, end)),
                    signature=_tolerant_signature(lines, start_line),
                    versions=versions,
                    parameters=parameters,
                ),
                start_line=start_line,
                indent_column=indent_column,
            )
        )
    return entries


def _tolerant_call_edges(
    *,
    tokens: list[tokenize.TokenInfo],
    entries: list[_TolerantEntry],
    path: str,
    lines: list[str],
    versions: ExecutionVersionBinding,
) -> list[CallEdge]:
    edges: list[CallEdge] = []
    for index, token in enumerate(tokens):
        if token.type != tokenize.OP or token.string != "(":
            continue
        name, name_index = _call_token_name(tokens, index)
        if name is None or name_index is None:
            continue
        caller = next(
            (
                entry
                for entry in reversed(entries)
                if entry.start_line <= token.start[0]
                and entry.indent_column <= token.start[1]
            ),
            None,
        )
        caller_id = caller.symbol.symbol_id if caller else f"{path}:module"
        start = _token_position_to_byte(tokens[name_index], lines)
        end = _token_position_to_byte(token, lines)
        edges.append(
            CallEdge(
                caller_symbol_id=caller_id,
                callee_name=name,
                file_path=path,
                byte_range=ByteRange(start, max(start, end)),
                versions=versions,
            )
        )
    return edges


def _next_significant(tokens: list[tokenize.TokenInfo], index: int) -> int | None:
    while index < len(tokens):
        token = tokens[index]
        if token.type not in {
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.NL,
            tokenize.NEWLINE,
            tokenize.COMMENT,
        }:
            return index
        index += 1
    return None


def _call_token_name(
    tokens: list[tokenize.TokenInfo],
    open_index: int,
) -> tuple[str | None, int | None]:
    index = open_index - 1
    parts: list[str] = []
    expected_name = True
    while index >= 0:
        token = tokens[index]
        if token.type == tokenize.NAME and expected_name:
            parts.append(token.string)
            expected_name = False
        elif token.type == tokenize.OP and token.string == "." and not expected_name:
            expected_name = True
        else:
            break
        index -= 1
    if not parts or expected_name:
        return None, None
    return ".".join(reversed(parts)), index + 1


def _token_position_to_byte(token: tokenize.TokenInfo, lines: list[str]) -> int:
    return _line_column_to_byte(lines, token.start[0], token.start[1])


def _tolerant_signature(lines: list[str], line_number: int) -> str | None:
    if not lines or line_number > len(lines):
        return None
    return lines[line_number - 1].strip()[:4096] or None


def _parameters(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[str, ...]:
    parameters = [argument.arg for argument in node.args.posonlyargs]
    parameters.extend(argument.arg for argument in node.args.args)
    if node.args.vararg is not None:
        parameters.append(f"*{node.args.vararg.arg}")
    parameters.extend(argument.arg for argument in node.args.kwonlyargs)
    if node.args.kwarg is not None:
        parameters.append(f"**{node.args.kwarg.arg}")
    return tuple(parameters)


def _tolerant_parameters(
    tokens: list[tokenize.TokenInfo],
    name_index: int,
) -> tuple[str, ...]:
    open_index = _next_significant(tokens, name_index + 1)
    if open_index is None or tokens[open_index].string != "(":
        return ()
    depth = 0
    groups: list[list[tokenize.TokenInfo]] = [[]]
    for token in tokens[open_index:]:
        if token.string == "(":
            depth += 1
            if depth == 1:
                continue
        elif token.string == ")":
            depth -= 1
            if depth == 0:
                break
        if depth == 1 and token.string == ",":
            groups.append([])
            continue
        if depth >= 1:
            groups[-1].append(token)
    parameters: list[str] = []
    for group in groups:
        significant = [
            token
            for token in group
            if token.type not in {tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT}
        ]
        if not significant:
            continue
        prefix = ""
        if significant[0].string in {"*", "**"}:
            prefix = significant.pop(0).string
        name = next(
            (token.string for token in significant if token.type == tokenize.NAME),
            None,
        )
        if name is not None:
            parameters.append(f"{prefix}{name}")
    return tuple(parameters)


def _byte_to_line(lines: list[str], byte_offset: int) -> int:
    consumed = 0
    for index, line in enumerate(lines, start=1):
        consumed += len(line.encode("utf-8"))
        if consumed >= byte_offset:
            return index
    return max(1, len(lines))


def _token_error_position(exc: BaseException) -> tuple[int, int]:
    args = getattr(exc, "args", ())
    if len(args) > 1 and isinstance(args[1], tuple) and len(args[1]) == 2:
        return max(1, int(args[1][0])), max(0, int(args[1][1]))
    return 1, 0
