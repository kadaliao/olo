# pylint: disable=E1101

"""Parse tree transformation module.

Transforms Python source code into an abstract syntax tree (AST)
defined in the ast module.

The simplest ways to invoke this module are via parse and parseFile.
parse(buf) -> AST
parseFile(path) -> AST
"""

# Original version written by Greg Stein (gstein@lyra.org)
#                         and Bill Tutt (rassilon@lima.mudlib.org)
# February 1997.
#
# Modifications and improvements for Python 2.0 by Jeremy Hylton and
# Mark Hammond
#
# Some fixes to try to have correct line number on almost all nodes
# (except Module, Discard and Stmt) added by Sylvain Thenault
#
# Portions of this file are:
# Copyright (C) 1997-1998 Greg Stein. All Rights Reserved.
#
# This module is provided under a BSD-ish license. See
#   http://www.opensource.org/licenses/bsd-license.html
# and replace OWNER, ORGANIZATION, and YEAR as appropriate.

from __future__ import absolute_import, print_function

import parser
import symbol
import token

from olo.libs.compiler.ast import (
    nodes, Pass, Module, Stmt, Expression, Getattr,
    Decorators, Discard, Function, Lambda, Class,
    Assign, AugAssign, Print, Printnl, Break, Continue,
    Return, Const, Yield, Raise, Import, From,
    Global, Exec, Assert, If, While, For,
    Tuple, List, IfExp, Or, And, Not, Compare,
    Bitand, Bitor, Bitxor, LeftShift, RightShift,
    Add, Sub, Mul, Div, Mod, FloorDiv,
    UnaryAdd, UnarySub, Invert, Power,
    Dict, Backquote, Name, TryExcept, TryFinally,
    With, Slice, Subscript, AssTuple, AssName, AssAttr,
    AssList, ListComp, ListCompFor, ListCompIf, DictComp,
    SetComp, GenExprFor, GenExprIf, GenExpr, GenExprInner,
    Set, CallFunc, Keyword, Sliceobj,
)
from olo.libs.compiler.consts import (
    CO_VARARGS, CO_VARKEYWORDS, OP_ASSIGN, OP_DELETE, OP_APPLY
)

from olo.compat import PY2, unicode, str_types

# Python 2.6 compatibility fix
if not hasattr(symbol, 'testlist_comp'):
    symbol.testlist_comp = symbol.testlist_gexp
if not hasattr(symbol, 'comp_iter'):
    symbol.comp_iter = symbol.gen_iter
if not hasattr(symbol, 'comp_for'):
    symbol.comp_for = symbol.gen_for
if not hasattr(symbol, 'comp_if'):
    symbol.comp_if = symbol.gen_if

atom_expr = getattr(symbol, 'atom_expr', None)


class WalkerError(Exception):
    pass


def parseFile(path):
    with open(path, "U") as f:
        # XXX The parser API tolerates files without a trailing newline,
        # but not strings without a trailing newline.  Always add an extra
        # newline to the file contents, since we're going through the string
        # version of the API.
        src = f.read() + "\n"
    return parse(src)


def parse(buf, mode="exec"):
    if mode in ["exec", "single"]:
        return Transformer().parsesuite(buf)
    elif mode == "eval":
        return Transformer().parseexpr(buf)
    else:
        raise ValueError(
            "compile() arg 3 must be 'exec' or 'eval' or 'single'")


def asList(nodes):
    l = []
    for item in nodes:
        if hasattr(item, "asList"):
            l.append(item.asList())
        elif isinstance(item, tuple):
            l.append(tuple(asList(item)))
        elif isinstance(item, list):
            l.append(asList(item))
        else:
            l.append(item)
    return l


def extractLineNo(ast):
    if not isinstance(ast[1], tuple):
        # get a terminal node
        return ast[2]
    for child in ast[1:]:
        if isinstance(child, tuple):
            lineno = extractLineNo(child)
            if lineno is not None:
                return lineno


def Node(*args):
    kind = args[0]
    if kind not in nodes:
        raise WalkerError("Can't find appropriate Node type: %s" % str(args))
    try:
        return nodes[kind](*args[1:])
    except TypeError:
        print(nodes[kind], len(args), args)
        raise


class Transformer:
    """Utility object for transforming Python parse trees.

    Exposes the following methods:
        tree = transform(ast_tree)
        tree = parsesuite(text)
        tree = parseexpr(text)
        tree = parsefile(fileob | filename)
    """

    def atom_expr(self, nodelist):
        atom_nodelist = nodelist[0]
        assert atom_nodelist[0] == symbol.atom, atom_nodelist[0]
        node = self.atom(atom_nodelist[1:])
        for i in range(1, len(nodelist)):
            elt = nodelist[i]
            node = self.com_apply_trailer(node, elt)
        return node

    def __init__(self):
        self._dispatch = {}
        for value, name in symbol.sym_name.items():
            if hasattr(self, name):
                self._dispatch[value] = getattr(self, name)
        self._dispatch[token.NEWLINE] = self.com_NEWLINE
        self._atom_dispatch = {token.LPAR: self.atom_lpar,
                               token.LSQB: self.atom_lsqb,
                               token.LBRACE: self.atom_lbrace,
                               token.NUMBER: self.atom_number,
                               token.STRING: self.atom_string,
                               token.NAME: self.atom_name,
                               }
        if PY2:
            self._atom_dispatch[token.BACKQUOTE] = self.atom_backquote
        if not PY2:
            self._atom_dispatch.update({
                token.ELLIPSIS: self.atom_ellipsis
            })
        self.encoding = None

    def transform(self, tree):
        """Transform an AST into a modified parse tree."""
        if not isinstance(tree, (tuple, list)):
            tree = parser.st2tuple(tree, line_info=1)
        return self.compile_node(tree)

    def parsesuite(self, text):
        """Return a modified parse tree for the given suite text."""
        return self.transform(parser.suite(text))

    def parseexpr(self, text):
        """Return a modified parse tree for the given expression text."""
        return self.transform(parser.expr(text))

    def parsefile(self, file):
        """Return a modified parse tree for the contents of the given file."""
        if isinstance(file, str_types):
            file = open(file)
        return self.parsesuite(file.read())

    # --------------------------------------------------------------
    #
    # PRIVATE METHODS
    #

    def compile_node(self, node):
        #  emit a line-number node?
        n = node[0]

        if n == symbol.encoding_decl:
            self.encoding = node[2]
            node = node[1]
            n = node[0]

        if n == symbol.single_input:
            return self.single_input(node[1:])
        if n == symbol.file_input:
            return self.file_input(node[1:])
        if n == symbol.eval_input:
            return self.eval_input(node[1:])
        if n == symbol.lambdef:
            return self.lambdef(node[1:])
        if n == symbol.funcdef:
            return self.funcdef(node[1:])
        if n == symbol.classdef:
            return self.classdef(node[1:])

        raise WalkerError('unexpected node type: %r' % n)

    def single_input(self, node):
        #  do we want to do anything about being "interactive" ?

        # NEWLINE | simple_stmt | compound_stmt NEWLINE
        n = node[0][0]
        if n != token.NEWLINE:
            return self.com_stmt(node[0])

        return Pass()

    def file_input(self, nodelist):
        doc = self.get_docstring(nodelist, symbol.file_input)
        i = 1 if doc is not None else 0
        stmts = []
        for node in nodelist[i:]:
            if node[0] not in [token.ENDMARKER, token.NEWLINE]:
                self.com_append_stmt(stmts, node)
        return Module(doc, Stmt(stmts))

    def eval_input(self, nodelist):
        # from the built-in function input()
        # is this sufficient?
        return Expression(self.com_node(nodelist[0]))

    def decorator_name(self, nodelist):
        listlen = len(nodelist)
        assert listlen >= 1 and listlen % 2 == 1

        item = self.atom_name(nodelist)
        for i in range(1, listlen, 2):
            assert nodelist[i][0] == token.DOT
            assert nodelist[i + 1][0] == token.NAME
            item = Getattr(item, nodelist[i + 1][1])
        return item

    def decorator(self, nodelist):
        # '@' dotted_name [ '(' [arglist] ')' ]
        assert len(nodelist) in (3, 5, 6)
        assert nodelist[0][0] == token.AT
        assert nodelist[-1][0] == token.NEWLINE

        assert nodelist[1][0] == symbol.dotted_name
        funcname = self.decorator_name(nodelist[1][1:])

        if len(nodelist) > 3:
            assert nodelist[2][0] == token.LPAR
            return self.com_call_function(funcname, nodelist[3])
        else:
            return funcname

    def decorators(self, nodelist):
        # decorators: decorator ([NEWLINE] decorator)* NEWLINE
        items = []
        for dec_nodelist in nodelist:
            assert dec_nodelist[0] == symbol.decorator
            items.append(self.decorator(dec_nodelist[1:]))
        return Decorators(items)

    def decorated(self, nodelist):
        assert nodelist[0][0] == symbol.decorators
        if nodelist[1][0] == symbol.funcdef:
            n = [nodelist[0]] + list(nodelist[1][1:])
            return self.funcdef(n)
        elif nodelist[1][0] == symbol.classdef:
            decorators = self.decorators(nodelist[0][1:])
            cls = self.classdef(nodelist[1][1:])
            cls.decorators = decorators
            return cls
        raise WalkerError()

    def funcdef(self, nodelist):
        #                    -6   -5    -4         -3  -2    -1
        # funcdef: [decorators] 'def' NAME parameters ':' suite
        # parameters: '(' [varargslist] ')'

        if len(nodelist) == 6:
            assert nodelist[0][0] == symbol.decorators
            decorators = self.decorators(nodelist[0][1:])
        else:
            assert len(nodelist) == 5
            decorators = None

        lineno = nodelist[-4][2]
        name = nodelist[-4][1]
        args = nodelist[-3][2]

        if args[0] == symbol.varargslist:
            names, defaults, flags = self.com_arglist(args[1:])
        else:
            names = defaults = ()
            flags = 0
        doc = self.get_docstring(nodelist[-1])

        # code for function
        code = self.com_node(nodelist[-1])

        if doc is not None:
            assert isinstance(code, Stmt)
            assert isinstance(code.nodes[0], Discard)
            del code.nodes[0]
        return Function(decorators, name, names, defaults, flags, doc, code,
                        lineno=lineno)

    def lambdef(self, nodelist):
        # lambdef: 'lambda' [varargslist] ':' test
        if nodelist[2][0] == symbol.varargslist:
            names, defaults, flags = self.com_arglist(nodelist[2][1:])
        else:
            names = defaults = ()
            flags = 0

        # code for lambda
        code = self.com_node(nodelist[-1])

        return Lambda(names, defaults, flags, code, lineno=nodelist[1][2])

    old_lambdef = lambdef

    def classdef(self, nodelist):
        # classdef: 'class' NAME ['(' [testlist] ')'] ':' suite

        name = nodelist[1][1]
        doc = self.get_docstring(nodelist[-1])
        if nodelist[2][0] == token.COLON:
            bases = []
        elif nodelist[3][0] == token.RPAR:
            bases = []
        else:
            bases = self.com_bases(nodelist[3])

        # code for class
        code = self.com_node(nodelist[-1])

        if doc is not None:
            assert isinstance(code, Stmt)
            assert isinstance(code.nodes[0], Discard)
            del code.nodes[0]

        return Class(name, bases, doc, code, lineno=nodelist[1][2])

    def stmt(self, nodelist):
        return self.com_stmt(nodelist[0])

    small_stmt = stmt
    flow_stmt = stmt
    compound_stmt = stmt

    def simple_stmt(self, nodelist):
        # small_stmt (';' small_stmt)* [';'] NEWLINE
        stmts = []
        for i in range(0, len(nodelist), 2):
            self.com_append_stmt(stmts, nodelist[i])
        return Stmt(stmts)

    def parameters(self, nodelist):
        raise WalkerError()

    def varargslist(self, nodelist):
        raise WalkerError()

    def fpdef(self, nodelist):
        raise WalkerError()

    def fplist(self, nodelist):
        raise WalkerError()

    def dotted_name(self, nodelist):
        raise WalkerError()

    def comp_op(self, nodelist):
        raise WalkerError()

    def trailer(self, nodelist):
        raise WalkerError()

    def sliceop(self, nodelist):
        raise WalkerError()

    def argument(self, nodelist):
        raise WalkerError()

    # --------------------------------------------------------------
    #
    # STATEMENT NODES  (invoked by com_node())
    #

    def expr_stmt(self, nodelist):
        # augassign testlist | testlist ('=' testlist)*
        en = nodelist[-1]
        exprNode = self.lookup_node(en)(en[1:])
        if len(nodelist) == 1:
            return Discard(exprNode, lineno=exprNode.lineno)
        if nodelist[1][0] == token.EQUAL:
            nodesl = [
                self.com_assign(nodelist[i], OP_ASSIGN)
                for i in range(0, len(nodelist) - 2, 2)
            ]

            return Assign(nodesl, exprNode, lineno=nodelist[1][2])
        else:
            lval = self.com_augassign(nodelist[0])
            op = self.com_augassign_op(nodelist[1])
            return AugAssign(lval, op[1], exprNode, lineno=op[2])
        raise WalkerError("can't get here")

    def print_stmt(self, nodelist):
        if len(nodelist) == 1:
            start = 1
            dest = None
        elif nodelist[1][0] == token.RIGHTSHIFT:
            assert len(nodelist) == 3 \
                   or nodelist[3][0] == token.COMMA
            dest = self.com_node(nodelist[2])
            start = 4
        else:
            dest = None
            start = 1
        items = [self.com_node(nodelist[i]) for i in range(start, len(nodelist), 2)]
        if nodelist[-1][0] == token.COMMA:
            return Print(items, dest, lineno=nodelist[0][2])
        return Printnl(items, dest, lineno=nodelist[0][2])

    def del_stmt(self, nodelist):
        return self.com_assign(nodelist[1], OP_DELETE)

    def pass_stmt(self, nodelist):
        return Pass(lineno=nodelist[0][2])

    def break_stmt(self, nodelist):
        return Break(lineno=nodelist[0][2])

    def continue_stmt(self, nodelist):
        return Continue(lineno=nodelist[0][2])

    def return_stmt(self, nodelist):
        # return: [testlist]
        if len(nodelist) < 2:
            return Return(Const(None), lineno=nodelist[0][2])
        return Return(self.com_node(nodelist[1]), lineno=nodelist[0][2])

    def yield_stmt(self, nodelist):
        expr = self.com_node(nodelist[0])
        return Discard(expr, lineno=expr.lineno)

    def yield_expr(self, nodelist):
        value = self.com_node(nodelist[1]) if len(nodelist) > 1 else Const(None)
        return Yield(value, lineno=nodelist[0][2])

    def raise_stmt(self, nodelist):
        # raise: [test [',' test [',' test]]]
        expr3 = self.com_node(nodelist[5]) if len(nodelist) > 5 else None
        expr2 = self.com_node(nodelist[3]) if len(nodelist) > 3 else None
        expr1 = self.com_node(nodelist[1]) if len(nodelist) > 1 else None
        return Raise(expr1, expr2, expr3, lineno=nodelist[0][2])

    def import_stmt(self, nodelist):
        # import_stmt: import_name | import_from
        assert len(nodelist) == 1
        return self.com_node(nodelist[0])

    def import_name(self, nodelist):
        # import_name: 'import' dotted_as_names
        return Import(self.com_dotted_as_names(nodelist[1]),
                      lineno=nodelist[0][2])

    def import_from(self, nodelist):
        # import_from: 'from' ('.'* dotted_name | '.') 'import' ('*' |
        #    '(' import_as_names ')' | import_as_names)
        assert nodelist[0][1] == 'from'
        idx = 1
        while nodelist[idx][1] == '.':
            idx += 1
        level = idx - 1
        if nodelist[idx][0] == symbol.dotted_name:
            fromname = self.com_dotted_name(nodelist[idx])
            idx += 1
        else:
            fromname = ""
        assert nodelist[idx][1] == 'import'
        if nodelist[idx + 1][0] == token.STAR:
            return From(fromname, [('*', None)], level,
                        lineno=nodelist[0][2])
        node = nodelist[idx + 1 + (nodelist[idx + 1][0] == token.LPAR)]
        return From(fromname, self.com_import_as_names(node), level,
                    lineno=nodelist[0][2])

    def global_stmt(self, nodelist):
        # global: NAME (',' NAME)*
        names = [nodelist[i][1] for i in range(1, len(nodelist), 2)]
        return Global(names, lineno=nodelist[0][2])

    def exec_stmt(self, nodelist):
        # exec_stmt: 'exec' expr ['in' expr [',' expr]]
        expr1 = self.com_node(nodelist[1])
        if len(nodelist) >= 4:
            expr2 = self.com_node(nodelist[3])
            expr3 = self.com_node(nodelist[5]) if len(nodelist) >= 6 else None
        else:
            expr2 = expr3 = None

        return Exec(expr1, expr2, expr3, lineno=nodelist[0][2])

    def assert_stmt(self, nodelist):
        # 'assert': test, [',' test]
        expr1 = self.com_node(nodelist[1])
        expr2 = self.com_node(nodelist[3]) if (len(nodelist) == 4) else None
        return Assert(expr1, expr2, lineno=nodelist[0][2])

    def if_stmt(self, nodelist):
        # if: test ':' suite ('elif' test ':' suite)* ['else' ':' suite]
        tests = []
        for i in range(0, len(nodelist) - 3, 4):
            testNode = self.com_node(nodelist[i + 1])
            suiteNode = self.com_node(nodelist[i + 3])
            tests.append((testNode, suiteNode))

        elseNode = self.com_node(nodelist[-1]) if len(nodelist) % 4 == 3 else None
        return If(tests, elseNode, lineno=nodelist[0][2])

    def while_stmt(self, nodelist):
        # 'while' test ':' suite ['else' ':' suite]

        testNode = self.com_node(nodelist[1])
        bodyNode = self.com_node(nodelist[3])

        elseNode = self.com_node(nodelist[6]) if len(nodelist) > 4 else None
        return While(testNode, bodyNode, elseNode, lineno=nodelist[0][2])

    def for_stmt(self, nodelist):
        # 'for' exprlist 'in' exprlist ':' suite ['else' ':' suite]

        assignNode = self.com_assign(nodelist[1], OP_ASSIGN)
        listNode = self.com_node(nodelist[3])
        bodyNode = self.com_node(nodelist[5])

        elseNode = self.com_node(nodelist[8]) if len(nodelist) > 8 else None
        return For(assignNode, listNode, bodyNode, elseNode,
                   lineno=nodelist[0][2])

    def try_stmt(self, nodelist):
        return self.com_try_except_finally(nodelist)

    def with_stmt(self, nodelist):
        return self.com_with(nodelist)

    def with_var(self, nodelist):
        return self.com_with_var(nodelist)

    def suite(self, nodelist):
        # simple_stmt | NEWLINE INDENT NEWLINE* (stmt NEWLINE*)+ DEDENT
        if len(nodelist) == 1:
            return self.com_stmt(nodelist[0])

        stmts = []
        for node in nodelist:
            if node[0] == symbol.stmt:
                self.com_append_stmt(stmts, node)
        return Stmt(stmts)

    # --------------------------------------------------------------
    #
    # EXPRESSION NODES  (invoked by com_node())
    #

    def testlist(self, nodelist):
        # testlist: expr (',' expr)* [',']
        # testlist_safe: test [(',' test)+ [',']]
        # exprlist: expr (',' expr)* [',']
        return self.com_binary(Tuple, nodelist)

    def testlist_star_expr(self, nodelist):
        return self.com_binary(Tuple, nodelist)

    def star_expr(self, *args):
        raise NotImplementedError

    testlist_safe = testlist  # XXX
    testlist1 = testlist
    exprlist = testlist

    def testlist_comp(self, nodelist):
        # test ( comp_for | (',' test)* [','] )
        assert nodelist[0][0] == symbol.test
        if len(nodelist) == 2 and nodelist[1][0] == symbol.comp_for:
            test = self.com_node(nodelist[0])
            return self.com_generator_expression(test, nodelist[1])
        return self.testlist(nodelist)

    testlist_gexp = testlist_comp  # Python 2.6 compatibility fix

    def test(self, nodelist):
        # or_test ['if' or_test 'else' test] | lambdef
        if len(nodelist) == 1 and nodelist[0][0] == symbol.lambdef:
            return self.lambdef(nodelist[0])
        then = self.com_node(nodelist[0])
        if len(nodelist) > 1:
            assert len(nodelist) == 5
            assert nodelist[1][1] == 'if'
            assert nodelist[3][1] == 'else'
            test = self.com_node(nodelist[2])
            else_ = self.com_node(nodelist[4])
            return IfExp(test, then, else_, lineno=nodelist[1][2])
        return then

    def or_test(self, nodelist):
        # and_test ('or' and_test)* | lambdef
        if len(nodelist) == 1 and nodelist[0][0] == symbol.lambdef:
            return self.lambdef(nodelist[0])
        return self.com_binary(Or, nodelist)
    old_test = or_test
    test_nocond = old_test

    def and_test(self, nodelist):
        # not_test ('and' not_test)*
        return self.com_binary(And, nodelist)

    def not_test(self, nodelist):
        # 'not' not_test | comparison
        result = self.com_node(nodelist[-1])
        if len(nodelist) == 2:
            return Not(result, lineno=nodelist[0][2])
        return result

    def comparison(self, nodelist):
        # comparison: expr (comp_op expr)*
        node = self.com_node(nodelist[0])
        if len(nodelist) == 1:
            return node

        results = []
        for i in range(2, len(nodelist), 2):
            nl = nodelist[i-1]

            # comp_op: '<' | '>' | '=' | '>=' | '<=' | '<>' | '!=' | '=='
            #          | 'in' | 'not' 'in' | 'is' | 'is' 'not'
            n = nl[1]
            if n[0] == token.NAME:
                type = n[1]
                if len(nl) == 3:
                    type = 'not in' if type == 'not' else 'is not'
            else:
                type = _cmp_types[n[0]]

            lineno = nl[1][2]
            results.append((type, self.com_node(nodelist[i])))

        # we need a special "compare" node so that we can distinguish
        #   3 < x < 5   from    (3 < x) < 5
        # the two have very different semantics and results (note that the
        # latter form is always true)

        return Compare(node, results, lineno=lineno)

    def expr(self, nodelist):
        # xor_expr ('|' xor_expr)*
        return self.com_binary(Bitor, nodelist)

    def xor_expr(self, nodelist):
        # xor_expr ('^' xor_expr)*
        return self.com_binary(Bitxor, nodelist)

    def and_expr(self, nodelist):
        # xor_expr ('&' xor_expr)*
        return self.com_binary(Bitand, nodelist)

    def shift_expr(self, nodelist):
        # shift_expr ('<<'|'>>' shift_expr)*
        node = self.com_node(nodelist[0])
        for i in range(2, len(nodelist), 2):
            right = self.com_node(nodelist[i])
            if nodelist[i-1][0] == token.LEFTSHIFT:
                node = LeftShift([node, right], lineno=nodelist[1][2])
            elif nodelist[i-1][0] == token.RIGHTSHIFT:
                node = RightShift([node, right], lineno=nodelist[1][2])
            else:
                raise ValueError("unexpected token: %s" % nodelist[i-1][0])
        return node

    def arith_expr(self, nodelist):
        node = self.com_node(nodelist[0])
        for i in range(2, len(nodelist), 2):
            right = self.com_node(nodelist[i])
            if nodelist[i-1][0] == token.PLUS:
                node = Add([node, right], lineno=nodelist[1][2])
            elif nodelist[i-1][0] == token.MINUS:
                node = Sub([node, right], lineno=nodelist[1][2])
            else:
                raise ValueError("unexpected token: %s" % nodelist[i-1][0])
        return node

    def term(self, nodelist):
        node = self.com_node(nodelist[0])
        for i in range(2, len(nodelist), 2):
            right = self.com_node(nodelist[i])
            t = nodelist[i-1][0]
            if t == token.STAR:
                node = Mul([node, right])
            elif t == token.SLASH:
                node = Div([node, right])
            elif t == token.PERCENT:
                node = Mod([node, right])
            elif t == token.DOUBLESLASH:
                node = FloorDiv([node, right])
            else:
                raise ValueError("unexpected token: %s" % t)
            node.lineno = nodelist[1][2]
        return node

    def factor(self, nodelist):
        elt = nodelist[0]
        t = elt[0]
        node = self.lookup_node(nodelist[-1])(nodelist[-1][1:])
        # need to handle (unary op)constant here...
        if t == token.PLUS:
            return UnaryAdd(node, lineno=elt[2])
        elif t == token.MINUS:
            return UnarySub(node, lineno=elt[2])
        elif t == token.TILDE:
            node = Invert(node, lineno=elt[2])
        return node

    def power(self, nodelist):
        # power: atom trailer* ('**' factor)*
        node = self.com_node(nodelist[0])
        for i in range(1, len(nodelist)):
            elt = nodelist[i]
            if elt[0] == token.DOUBLESTAR:
                return Power([node, self.com_node(nodelist[i+1])],
                             lineno=elt[2])

            node = self.com_apply_trailer(node, elt)

        return node

    def atom(self, nodelist):
        return self._atom_dispatch[nodelist[0][0]](nodelist)

    def atom_lpar(self, nodelist):
        if nodelist[1][0] == token.RPAR:
            return Tuple((), lineno=nodelist[0][2])
        return self.com_node(nodelist[1])

    def atom_lsqb(self, nodelist):
        if nodelist[1][0] == token.RSQB:
            return List((), lineno=nodelist[0][2])
        return self.com_list_constructor(nodelist[1])

    def atom_lbrace(self, nodelist):
        if nodelist[1][0] == token.RBRACE:
            return Dict((), lineno=nodelist[0][2])
        return self.com_dictorsetmaker(nodelist[1])

    def atom_backquote(self, nodelist):
        return Backquote(self.com_node(nodelist[1]))

    def atom_ellipsis(self, nodelist):
        return Ellipsis()

    def atom_number(self, nodelist):
        # need to verify this matches compile.c
        k = eval(nodelist[0][1])
        return Const(k, lineno=nodelist[0][2])

    def decode_literal(self, lit):
        if not self.encoding:
            return eval(lit)
        # this is particularly fragile & a bit of a
        # hack... changes in compile.c:parsestr and
        # tokenizer.c must be reflected here.
        if self.encoding not in ['utf-8', 'iso-8859-1']:
            lit = unicode(lit, 'utf-8').encode(self.encoding)
        return eval("# coding: %s\n%s" % (self.encoding, lit))

    def atom_string(self, nodelist):
        k = ''.join(self.decode_literal(node[1]) for node in nodelist)
        return Const(k, lineno=nodelist[0][2])

    def atom_name(self, nodelist):
        return Name(nodelist[0][1], lineno=nodelist[0][2])

    # --------------------------------------------------------------
    #
    # INTERNAL PARSING UTILITIES
    #

    # The use of com_node() introduces a lot of extra stack frames,
    # enough to cause a stack overflow compiling test.test_parser with
    # the standard interpreter recursionlimit.  The com_node() is a
    # convenience function that hides the dispatch details, but comes
    # at a very high cost.  It is more efficient to dispatch directly
    # in the callers.  In these cases, use lookup_node() and call the
    # dispatched node directly.

    def lookup_node(self, node):
        return self._dispatch[node[0]]

    def com_node(self, node):
        # Note: compile.c has handling in com_node for del_stmt, pass_stmt,
        #       break_stmt, stmt, small_stmt, flow_stmt, simple_stmt,
        #       and compound_stmt.
        #       We'll just dispatch them.
        return self._dispatch[node[0]](node[1:])

    def com_NEWLINE(self, *args):
        # A ';' at the end of a line can make a NEWLINE token appear
        # here, Render it harmless. (genc discards ('discard',
        # ('const', xxxx)) Nodes)
        return Discard(Const(None))

    def com_arglist(self, nodelist):
        # varargslist:
        #     (fpdef ['=' test] ',')* ('*' NAME [',' '**' NAME] | '**' NAME)
        #   | fpdef ['=' test] (',' fpdef ['=' test])* [',']
        # fpdef: NAME | '(' fplist ')'
        # fplist: fpdef (',' fpdef)* [',']
        names = []
        defaults = []
        flags = 0

        i = 0
        while i < len(nodelist):
            node = nodelist[i]
            if node[0] in [token.STAR, token.DOUBLESTAR]:
                if node[0] == token.STAR:
                    node = nodelist[i+1]
                    if node[0] == token.NAME:
                        names.append(node[1])
                        flags |= CO_VARARGS
                        i += 3

                if i < len(nodelist):
                    # should be DOUBLESTAR
                    t = nodelist[i][0]
                    if t == token.DOUBLESTAR:
                        node = nodelist[i+1]
                    else:
                        raise ValueError("unexpected token: %s" % t)
                    names.append(node[1])
                    flags |= CO_VARKEYWORDS

                break

            # fpdef: NAME | '(' fplist ')'
            names.append(self.com_fpdef(node))

            i += 1
            if i < len(nodelist) and nodelist[i][0] == token.EQUAL:
                defaults.append(self.com_node(nodelist[i + 1]))
                i += 2
            elif len(defaults):
                # we have already seen an argument with default, but here
                # came one without
                raise SyntaxError(
                    "non-default argument follows default argument")

            # skip the comma
            i += 1

        return names, defaults, flags

    def com_fpdef(self, node):
        # fpdef: NAME | '(' fplist ')'
        return self.com_fplist(node[2]) if node[1][0] == token.LPAR else node[1][1]

    def com_fplist(self, node):
        # fplist: fpdef (',' fpdef)* [',']
        if len(node) == 2:
            return self.com_fpdef(node[1])
        list = [self.com_fpdef(node[i]) for i in range(1, len(node), 2)]
        return tuple(list)

    def com_dotted_name(self, node):
        # String together the dotted names and return the string
        name = ""
        for n in node:
            if isinstance(n, tuple) and n[0] == 1:
                name = name + n[1] + '.'
        return name[:-1]

    def com_dotted_as_name(self, node):
        assert node[0] == symbol.dotted_as_name
        node = node[1:]
        dot = self.com_dotted_name(node[0][1:])
        if len(node) == 1:
            return dot, None
        assert node[1][1] == 'as'
        assert node[2][0] == token.NAME
        return dot, node[2][1]

    def com_dotted_as_names(self, node):
        assert node[0] == symbol.dotted_as_names
        node = node[1:]
        names = [self.com_dotted_as_name(node[0])]
        for i in range(2, len(node), 2):
            names.append(self.com_dotted_as_name(node[i]))
        return names

    def com_import_as_name(self, node):
        assert node[0] == symbol.import_as_name
        node = node[1:]
        assert node[0][0] == token.NAME
        if len(node) == 1:
            return node[0][1], None
        assert node[1][1] == 'as', node
        assert node[2][0] == token.NAME
        return node[0][1], node[2][1]

    def com_import_as_names(self, node):
        assert node[0] == symbol.import_as_names
        node = node[1:]
        names = [self.com_import_as_name(node[0])]
        for i in range(2, len(node), 2):
            names.append(self.com_import_as_name(node[i]))
        return names

    def com_bases(self, node):
        return [self.com_node(node[i]) for i in range(1, len(node), 2)]

    def com_try_except_finally(self, nodelist):
        # ('try' ':' suite
        #  ((except_clause ':' suite)+ ['else' ':' suite] ['finally' ':' suite]
        #   | 'finally' ':' suite))

        if nodelist[3][0] == token.NAME:
            # first clause is a finally clause: only try-finally
            return TryFinally(self.com_node(nodelist[2]),
                              self.com_node(nodelist[5]),
                              lineno=nodelist[0][2])

        # tryexcept:  [TryNode, [except_clauses], elseNode)]
        clauses = []
        elseNode = None
        finallyNode = None
        for i in range(3, len(nodelist), 3):
            node = nodelist[i]
            if node[0] == symbol.except_clause:
                # except_clause: 'except' [expr [(',' | 'as') expr]] */
                if len(node) > 2:
                    expr1 = self.com_node(node[2])
                    expr2 = self.com_assign(node[4], OP_ASSIGN) if len(node) > 4 else None
                else:
                    expr1 = expr2 = None
                clauses.append((expr1, expr2, self.com_node(nodelist[i+2])))

            if node[0] == token.NAME:
                if node[1] == 'else':
                    elseNode = self.com_node(nodelist[i+2])
                elif node[1] == 'finally':
                    finallyNode = self.com_node(nodelist[i+2])
        try_except = TryExcept(self.com_node(nodelist[2]), clauses, elseNode,
                               lineno=nodelist[0][2])
        if finallyNode:
            return TryFinally(try_except, finallyNode, lineno=nodelist[0][2])
        else:
            return try_except

    def com_with(self, nodelist):
        # with_stmt: 'with' with_item (',' with_item)* ':' suite
        body = self.com_node(nodelist[-1])
        for i in range(len(nodelist) - 3, 0, -2):
            ret = self.com_with_item(nodelist[i], body, nodelist[0][2])
            if i == 1:
                return ret
            body = ret

    def com_with_item(self, nodelist, body, lineno):
        # with_item: test ['as' expr]
        var = self.com_assign(nodelist[3], OP_ASSIGN) if len(nodelist) == 4 else None
        expr = self.com_node(nodelist[1])
        return With(expr, var, body, lineno=lineno)

    def com_augassign_op(self, node):
        assert node[0] == symbol.augassign
        return node[1]

    def com_augassign(self, node):
        """Return node suitable for lvalue of augmented assignment

        Names, slices, and attributes are the only allowable nodes.
        """
        l = self.com_node(node)
        if l.__class__ in (Name, Slice, Subscript, Getattr):
            return l
        raise SyntaxError("can't assign to %s" % l.__class__.__name__)

    def com_assign(self, node, assigning):
        # return a node suitable for use as an "lvalue"
        # loop to avoid trivial recursion
        while 1:
            t = node[0]
            if t in (symbol.exprlist, symbol.testlist, symbol.testlist_comp) \
               or PY2 and t == symbol.testlist_safe:
                if len(node) > 2:
                    return self.com_assign_tuple(node, assigning)
                node = node[1]
            elif t in _assign_types:
                if len(node) > 2:
                    raise SyntaxError("can't assign to operator")
                node = node[1]
            elif t == symbol.power:
                if node[1][0] not in (symbol.atom, atom_expr):
                    raise SyntaxError("can't assign to operator")
                if len(node) > 2:
                    primary = self.com_node(node[1])
                    for i in range(2, len(node)-1):
                        ch = node[i]
                        if ch[0] == token.DOUBLESTAR:
                            raise SyntaxError("can't assign to operator")
                        primary = self.com_apply_trailer(primary, ch)
                    return self.com_assign_trailer(primary, node[-1],
                                                   assigning)
                node = node[1]
            elif t == atom_expr:
                if node[1][0] != symbol.atom:
                    raise SyntaxError("can't assign to operator")
                if len(node) > 2:
                    primary = self.com_node(node[1])
                    for i in range(2, len(node)-1):
                        ch = node[i]
                        primary = self.com_apply_trailer(primary, ch)
                    return self.com_assign_trailer(primary, node[-1],
                                                   assigning)
                node = node[1]
            elif t == symbol.atom:
                t = node[1][0]
                if t == token.LPAR:
                    node = node[2]
                    if node[0] == token.RPAR:
                        raise SyntaxError("can't assign to ()")
                elif t == token.LSQB:
                    node = node[2]
                    if node[0] == token.RSQB:
                        raise SyntaxError("can't assign to []")
                    return self.com_assign_list(node, assigning)
                elif t == token.NAME:
                    return self.com_assign_name(node[1], assigning)
                else:
                    raise SyntaxError("can't assign to literal")
            else:
                raise SyntaxError("bad assignment (%s)" % t)

    def com_assign_tuple(self, node, assigning):
        assigns = [self.com_assign(node[i], assigning) for i in range(1, len(node), 2)]
        return AssTuple(assigns, lineno=extractLineNo(node))

    def com_assign_list(self, node, assigning):
        assigns = []
        for i in range(1, len(node), 2):
            if i + 1 < len(node):
                if node[i + 1][0] == symbol.list_for:
                    raise SyntaxError("can't assign to list comprehension")
                assert node[i + 1][0] == token.COMMA, node[i + 1]
            assigns.append(self.com_assign(node[i], assigning))
        return AssList(assigns, lineno=extractLineNo(node))

    def com_assign_name(self, node, assigning):
        return AssName(node[1], assigning, lineno=node[2])

    def com_assign_trailer(self, primary, node, assigning):
        t = node[1][0]
        if t == token.DOT:
            return self.com_assign_attr(primary, node[2], assigning)
        if t == token.LSQB:
            return self.com_subscriptlist(primary, node[2], assigning)
        if t == token.LPAR:
            raise SyntaxError("can't assign to function call")
        raise SyntaxError("unknown trailer type: %s" % t)

    def com_assign_attr(self, primary, node, assigning):
        return AssAttr(primary, node[1], assigning, lineno=node[-1])

    def com_binary(self, constructor, nodelist):
        "Compile 'NODE (OP NODE)*' into (type, [ node1, ..., nodeN ])."
        l = len(nodelist)
        if l == 1:
            n = nodelist[0]
            return self.lookup_node(n)(n[1:])
        items = []
        for i in range(0, l, 2):
            n = nodelist[i]
            items.append(self.lookup_node(n)(n[1:]))
        return constructor(items, lineno=extractLineNo(nodelist))

    def com_stmt(self, node):
        result = self.lookup_node(node)(node[1:])
        assert result is not None
        if isinstance(result, Stmt):
            return result
        return Stmt([result])

    def com_append_stmt(self, stmts, node):
        result = self.lookup_node(node)(node[1:])
        assert result is not None
        if isinstance(result, Stmt):
            stmts.extend(result.nodes)
        else:
            stmts.append(result)

    def com_list_constructor(self, nodelist):
        # listmaker: test ( list_for | (',' test)* [','] )
        values = []
        for i in range(1, len(nodelist)):
            if PY2 and nodelist[i][0] == symbol.list_for:
                assert len(nodelist[i:]) == 1
                return self.com_list_comprehension(values[0],
                                                   nodelist[i])
            elif nodelist[i][0] == token.COMMA:
                continue
            values.append(self.com_node(nodelist[i]))
        return List(values, lineno=values[0].lineno)

    def com_list_comprehension(self, expr, node):
        return self.com_comprehension(expr, None, node, 'list')

    def com_comprehension(self, expr1, expr2, node, type):
        # list_iter: list_for | list_if
        # list_for: 'for' exprlist 'in' testlist [list_iter]
        # list_if: 'if' test [list_iter]

        # XXX should raise SyntaxError for assignment
        # XXX(avassalotti) Set and dict comprehensions should have generator
        #                  semantics. In other words, they shouldn't leak
        #                  variables outside of the comprehension's scope.

        lineno = node[1][2]
        fors = []
        while node:
            t = node[1][1]
            if t == 'for':
                assignNode = self.com_assign(node[2], OP_ASSIGN)
                compNode = self.com_node(node[4])
                newfor = ListCompFor(assignNode, compNode, [])
                newfor.lineno = node[1][2]
                fors.append(newfor)
                if len(node) == 5:
                    node = None
                elif type == 'list':
                    node = self.com_list_iter(node[5])
                else:
                    node = self.com_comp_iter(node[5])
            elif t == 'if':
                test = self.com_node(node[2])
                newif = ListCompIf(test, lineno=node[1][2])
                newfor.ifs.append(newif)
                if len(node) == 3:
                    node = None
                elif type == 'list':
                    node = self.com_list_iter(node[3])
                else:
                    node = self.com_comp_iter(node[3])
            else:
                raise SyntaxError(
                    "unexpected comprehension element: %s %d" % (node, lineno))
        if type == 'list':
            return ListComp(expr1, fors, lineno=lineno)
        elif type == 'set':
            return SetComp(expr1, fors, lineno=lineno)
        elif type == 'dict':
            return DictComp(expr1, expr2, fors, lineno=lineno)
        else:
            raise ValueError("unexpected comprehension type: " + repr(type))

    def com_list_iter(self, node):
        assert node[0] == symbol.list_iter
        return node[1]

    def com_comp_iter(self, node):
        assert node[0] == symbol.comp_iter
        return node[1]

    def com_generator_expression(self, expr, node):
        # comp_iter: comp_for | comp_if
        # comp_for: 'for' exprlist 'in' test [comp_iter]
        # comp_if: 'if' test [comp_iter]

        lineno = node[1][2]
        fors = []
        while node:
            t = node[1][1]
            if t == 'for':
                assignNode = self.com_assign(node[2], OP_ASSIGN)
                genNode = self.com_node(node[4])
                newfor = GenExprFor(assignNode, genNode, [],
                                    lineno=node[1][2])
                fors.append(newfor)
                node = None if (len(node)) == 5 else self.com_comp_iter(node[5])
            elif t == 'if':
                test = self.com_node(node[2])
                newif = GenExprIf(test, lineno=node[1][2])
                newfor.ifs.append(newif)
                node = None if len(node) == 3 else self.com_comp_iter(node[3])
            else:
                raise SyntaxError(
                    "unexpected generator expression element: %s %d" % (
                        node, lineno))
        fors[0].is_outmost = True
        return GenExpr(GenExprInner(expr, fors), lineno=lineno)

    def com_dictorsetmaker(self, nodelist):
        # noqa dictorsetmaker: ( (test ':' test (comp_for | (',' test ':' test)* [','])) |
        #                   (test (comp_for | (',' test)* [','])) )
        assert nodelist[0] == symbol.dictorsetmaker
        nodelist = nodelist[1:]
        if len(nodelist) == 1 or nodelist[1][0] == token.COMMA:
            # set literal
            items = [self.com_node(nodelist[i]) for i in range(0, len(nodelist), 2)]
            return Set(items, lineno=items[0].lineno)
        elif nodelist[1][0] == symbol.comp_for:
            # set comprehension
            expr = self.com_node(nodelist[0])
            return self.com_comprehension(expr, None, nodelist[1], 'set')
        elif len(nodelist) > 3 and nodelist[3][0] == symbol.comp_for:
            # dict comprehension
            assert nodelist[1][0] == token.COLON
            key = self.com_node(nodelist[0])
            value = self.com_node(nodelist[2])
            return self.com_comprehension(key, value, nodelist[3], 'dict')
        else:
            # dict literal
            items = [
                (self.com_node(nodelist[i]), self.com_node(nodelist[i + 2]))
                for i in range(0, len(nodelist), 4)
            ]

            return Dict(items, lineno=items[0][0].lineno)

    def com_apply_trailer(self, primaryNode, nodelist):
        t = nodelist[1][0]
        if t == token.LPAR:
            return self.com_call_function(primaryNode, nodelist[2])
        if t == token.DOT:
            return self.com_select_member(primaryNode, nodelist[2])
        if t == token.LSQB:
            return self.com_subscriptlist(primaryNode, nodelist[2], OP_APPLY)

        raise SyntaxError('unknown node type: %s' % t)

    def com_select_member(self, primaryNode, nodelist):
        if nodelist[0] != token.NAME:
            raise SyntaxError("member must be a name")
        return Getattr(primaryNode, nodelist[1], lineno=nodelist[2])

    def com_call_function(self, primaryNode, nodelist):
        if nodelist[0] == token.RPAR:
            return CallFunc(primaryNode, [], lineno=extractLineNo(nodelist))
        args = []
        kw = 0
        star_node = dstar_node = None
        len_nodelist = len(nodelist)
        i = 1
        while i < len_nodelist:
            node = nodelist[i]

            if node[0] == token.STAR:
                if star_node is not None:
                    raise SyntaxError('already have the varargs indentifier')
                star_node = self.com_node(nodelist[i+1])
                i += 3
                continue
            elif node[0] == token.DOUBLESTAR:
                if dstar_node is not None:
                    raise SyntaxError('already have the kwargs indentifier')
                dstar_node = self.com_node(nodelist[i+1])
                i += 3
                continue

            # positional or named parameters
            kw, result = self.com_argument(node, kw, star_node)

            if len_nodelist != 2 and isinstance(result, GenExpr) \
               and len(node) == 3 and node[2][0] == symbol.comp_for:
                # allow f(x for x in y), but reject f(x for x in y, 1)
                # should use f((x for x in y), 1) instead of f(x for x in y, 1)
                raise SyntaxError('generator expression needs parenthesis')

            args.append(result)
            i += 2

        return CallFunc(primaryNode, args, star_node, dstar_node,
                        lineno=extractLineNo(nodelist))

    def com_argument(self, nodelist, kw, star_node):
        if len(nodelist) == 3 and nodelist[2][0] == symbol.comp_for:
            test = self.com_node(nodelist[1])
            return 0, self.com_generator_expression(test, nodelist[2])
        if len(nodelist) == 2:
            if kw:
                raise SyntaxError("non-keyword arg after keyword arg")
            if star_node:
                raise SyntaxError(
                    "only named arguments may follow *expression")
            return 0, self.com_node(nodelist[1])
        assert len(nodelist) > 3, [kw, star_node, nodelist]
        result = self.com_node(nodelist[3])
        n = nodelist[1]
        while len(n) == 2 and n[0] != token.NAME:
            n = n[1]
        if n[0] != token.NAME:
            raise SyntaxError("keyword can't be an expression (%s)" % n[0])
        node = Keyword(n[1], result, lineno=n[2])
        return 1, node

    def com_subscriptlist(self, primary, nodelist, assigning):
        # slicing:      simple_slicing | extended_slicing
        # simple_slicing:   primary "[" short_slice "]"
        # extended_slicing: primary "[" slice_list "]"
        # slice_list:   slice_item ("," slice_item)* [","]

        # backwards compat slice for '[i:j]'
        if len(nodelist) == 2:
            sub = nodelist[1]
            if (sub[1][0] == token.COLON or
                (len(sub) > 2 and sub[2][0] == token.COLON)) and \
                    sub[-1][0] != symbol.sliceop:
                return self.com_slice(primary, sub, assigning)

        subscripts = [
            self.com_subscript(nodelist[i]) for i in range(1, len(nodelist), 2)
        ]

        return Subscript(primary, assigning, subscripts,
                         lineno=extractLineNo(nodelist))

    def com_subscript(self, node):
        # slice_item: expression | proper_slice | ellipsis
        ch = node[1]
        t = ch[0]
        if t == token.DOT and node[2][0] == token.DOT:
            return Ellipsis()
        if t == token.COLON or len(node) > 2:
            return self.com_sliceobj(node)
        return self.com_node(ch)

    def com_sliceobj(self, node):
        # proper_slice: short_slice | long_slice
        # short_slice:  [lower_bound] ":" [upper_bound]
        # long_slice:   short_slice ":" [stride]
        # lower_bound:  expression
        # upper_bound:  expression
        # stride:       expression
        #
        # Note: a stride may be further slicing...

        items = []

        if node[1][0] == token.COLON:
            items.append(Const(None))
            i = 2
        else:
            items.append(self.com_node(node[1]))
            # i == 2 is a COLON
            i = 3

        if i < len(node) and node[i][0] == symbol.test:
            items.append(self.com_node(node[i]))
            i += 1
        else:
            items.append(Const(None))

        # a short_slice has been built. look for long_slice now by looking
        # for strides...
        for j in range(i, len(node)):
            ch = node[j]
            if len(ch) == 2:
                items.append(Const(None))
            else:
                items.append(self.com_node(ch[2]))
        return Sliceobj(items, lineno=extractLineNo(node))

    def com_slice(self, primary, node, assigning):
        # short_slice:  [lower_bound] ":" [upper_bound]
        lower = upper = None
        if len(node) == 3:
            if node[1][0] == token.COLON:
                upper = self.com_node(node[2])
            else:
                lower = self.com_node(node[1])
        elif len(node) == 4:
            lower = self.com_node(node[1])
            upper = self.com_node(node[3])
        return Slice(primary, assigning, lower, upper,
                     lineno=extractLineNo(node))

    def get_docstring(self, node, n=None):
        if n is None:
            n = node[0]
            node = node[1:]
        if n == symbol.suite:
            if len(node) == 1:
                return self.get_docstring(node[0])
            for sub in node:
                if sub[0] == symbol.stmt:
                    return self.get_docstring(sub)
            return None
        if n == symbol.file_input:
            for sub in node:
                if sub[0] == symbol.stmt:
                    return self.get_docstring(sub)
            return None
        if n == symbol.atom:
            if node[0][0] == token.STRING:
                return ''.join(eval(t[1]) for t in node)
            return None
        if n in [symbol.stmt, symbol.simple_stmt, symbol.small_stmt]:
            return self.get_docstring(node[0])
        if n in _doc_nodes and len(node) == 1:
            return self.get_docstring(node[0])
        return None


_doc_nodes = [
    symbol.expr_stmt,
    symbol.testlist,
    symbol.test,
    symbol.or_test,
    symbol.and_test,
    symbol.not_test,
    symbol.comparison,
    symbol.expr,
    symbol.xor_expr,
    symbol.and_expr,
    symbol.shift_expr,
    symbol.arith_expr,
    symbol.term,
    symbol.factor,
    symbol.power,
    ]
if hasattr(symbol, 'testlist_safe'):
    _doc_nodes.append(symbol.testlist_safe)

# comp_op: '<' | '>' | '=' | '>=' | '<=' | '<>' | '!=' | '=='
#             | 'in' | 'not' 'in' | 'is' | 'is' 'not'
_cmp_types = {
    token.LESS: '<',
    token.GREATER: '>',
    token.EQEQUAL: '==',
    token.EQUAL: '==',
    token.LESSEQUAL: '<=',
    token.GREATEREQUAL: '>=',
    token.NOTEQUAL: '!=',
}

_legal_node_types = [
    symbol.funcdef,
    symbol.classdef,
    symbol.stmt,
    symbol.small_stmt,
    symbol.flow_stmt,
    symbol.simple_stmt,
    symbol.compound_stmt,
    symbol.expr_stmt,
    symbol.del_stmt,
    symbol.pass_stmt,
    symbol.break_stmt,
    symbol.continue_stmt,
    symbol.return_stmt,
    symbol.raise_stmt,
    symbol.import_stmt,
    symbol.global_stmt,
    symbol.assert_stmt,
    symbol.if_stmt,
    symbol.while_stmt,
    symbol.for_stmt,
    symbol.try_stmt,
    symbol.with_stmt,
    symbol.suite,
    symbol.testlist,
    symbol.test,
    symbol.and_test,
    symbol.not_test,
    symbol.comparison,
    symbol.exprlist,
    symbol.expr,
    symbol.xor_expr,
    symbol.and_expr,
    symbol.shift_expr,
    symbol.arith_expr,
    symbol.term,
    symbol.factor,
    symbol.power,
    symbol.atom,
    ]

if hasattr(symbol, 'yield_stmt'):
    _legal_node_types.append(symbol.yield_stmt)
if hasattr(symbol, 'yield_expr'):
    _legal_node_types.append(symbol.yield_expr)
if hasattr(symbol, 'print_stmt'):
    _legal_node_types.append(symbol.print_stmt)
if hasattr(symbol, 'exec_stmt'):
    _legal_node_types.append(symbol.print_stmt)
if hasattr(symbol, 'testlist_safe'):
    _legal_node_types.append(symbol.testlist_safe)

_assign_types = [
    symbol.test,
    symbol.or_test,
    symbol.and_test,
    symbol.not_test,
    symbol.comparison,
    symbol.expr,
    symbol.xor_expr,
    symbol.and_expr,
    symbol.shift_expr,
    symbol.arith_expr,
    symbol.term,
    symbol.factor,
    ]

_names = dict(symbol.sym_name.items())
for k, v in token.tok_name.items():
    _names[k] = v


def debug_tree(tree):
    l = []
    for elt in tree:
        if isinstance(elt, int):
            l.append(_names.get(elt, elt))
        elif isinstance(elt, str):
            l.append(elt)
        else:
            l.append(debug_tree(elt))
    return l
