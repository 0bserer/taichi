import numbers
from collections.abc import Iterable

import numpy as np
from taichi._lib import core as ti_core
from taichi.lang import expr, impl
from taichi.lang import ops as ops_mod
from taichi.lang import runtime_ops
from taichi.lang._ndarray import Ndarray, NdarrayHostAccess
from taichi.lang.common_ops import TaichiOperations
from taichi.lang.enums import Layout
from taichi.lang.exception import TaichiSyntaxError
from taichi.lang.field import Field, ScalarField, SNodeHostAccess
from taichi.lang.util import (cook_dtype, in_python_scope, python_scope,
                              taichi_scope, to_numpy_type, to_pytorch_type,
                              warning)
from taichi.types import primitive_types
from taichi.types.compound_types import CompoundType


class Matrix(TaichiOperations):
    """The matrix class.

    Args:
        n (Union[int, list, tuple, np.ndarray]): the first dimension of a matrix.
        m (int): the second dimension of a matrix.
        dt (DataType): the element data type.
    """
    is_taichi_class = True

    def __init__(self, n=1, m=1, dt=None, suppress_warning=False):
        self.local_tensor_proxy = None
        self.any_array_access = None
        self.grad = None
        self.dynamic_index_stride = None

        if isinstance(n, (list, tuple, np.ndarray)):
            if len(n) == 0:
                mat = []
            elif isinstance(n[0], Matrix):
                raise Exception(
                    'cols/rows required when using list of vectors')
            elif not isinstance(n[0], Iterable):  # now init a Vector
                if in_python_scope():
                    mat = [[x] for x in n]
                elif not impl.current_cfg().dynamic_index:
                    mat = [[impl.expr_init(x)] for x in n]
                else:
                    if not ti_core.is_extension_supported(
                            impl.current_cfg().arch,
                            ti_core.Extension.dynamic_index):
                        raise Exception(
                            f"Backend {impl.current_cfg().arch} doesn't support dynamic index"
                        )
                    if dt is None:
                        if isinstance(n[0], (int, np.integer)):
                            dt = impl.get_runtime().default_ip
                        elif isinstance(n[0], float):
                            dt = impl.get_runtime().default_fp
                        elif isinstance(n[0], expr.Expr):
                            dt = n[0].ptr.get_ret_type()
                            if dt == ti_core.DataType_unknown:
                                raise TypeError(
                                    'Element type of the matrix cannot be inferred. Please set dt instead for now.'
                                )
                        else:
                            raise Exception(
                                'dt required when using dynamic_index for local tensor'
                            )
                    self.local_tensor_proxy = impl.expr_init_local_tensor(
                        [len(n)], dt,
                        expr.make_expr_group([expr.Expr(x) for x in n]))
                    self.dynamic_index_stride = 1
                    mat = []
                    for i in range(len(n)):
                        mat.append(
                            list([
                                impl.make_tensor_element_expr(
                                    self.local_tensor_proxy,
                                    (impl.make_constant_expr_i32(i), ),
                                    (len(n), ), self.dynamic_index_stride)
                            ]))
            else:  # now init a Matrix
                if in_python_scope():
                    mat = [list(row) for row in n]
                elif not impl.current_cfg().dynamic_index:
                    mat = [[impl.expr_init(x) for x in row] for row in n]
                else:
                    if not ti_core.is_extension_supported(
                            impl.current_cfg().arch,
                            ti_core.Extension.dynamic_index):
                        raise Exception(
                            f"Backend {impl.current_cfg().arch} doesn't support dynamic index"
                        )
                    if dt is None:
                        if isinstance(n[0][0], (int, np.integer)):
                            dt = impl.get_runtime().default_ip
                        elif isinstance(n[0][0], float):
                            dt = impl.get_runtime().default_fp
                        elif isinstance(n[0][0], expr.Expr):
                            dt = n[0][0].ptr.get_ret_type()
                            if dt == ti_core.DataType_unknown:
                                raise TypeError(
                                    'Element type of the matrix cannot be inferred. Please set dt instead for now.'
                                )
                        else:
                            raise Exception(
                                'dt required when using dynamic_index for local tensor'
                            )
                    self.local_tensor_proxy = impl.expr_init_local_tensor(
                        [len(n), len(n[0])], dt,
                        expr.make_expr_group(
                            [expr.Expr(x) for row in n for x in row]))
                    self.dynamic_index_stride = 1
                    mat = []
                    for i in range(len(n)):
                        mat.append([])
                        for j in range(len(n[0])):
                            mat[i].append(
                                impl.make_tensor_element_expr(
                                    self.local_tensor_proxy,
                                    (impl.make_constant_expr_i32(i),
                                     impl.make_constant_expr_i32(j)),
                                    (len(n), len(n[0])),
                                    self.dynamic_index_stride))
            self.n = len(mat)
            if len(mat) > 0:
                self.m = len(mat[0])
            else:
                self.m = 1
            self.entries = [x for row in mat for x in row]

        else:
            if dt is None:
                # create a local matrix with specific (n, m)
                self.entries = [impl.expr_init(None) for i in range(n * m)]
                self.n = n
                self.m = m
            else:
                raise ValueError(
                    "Declaring matrix fields using `ti.Matrix(n, m, dt, shape)` is no longer supported. "
                    "Use `ti.Matrix.field(n, m, dtype, shape)` instead.")

        if self.n * self.m > 32 and not suppress_warning:
            warning(
                f'Taichi matrices/vectors with {self.n}x{self.m} > 32 entries are not suggested.'
                ' Matrices/vectors will be automatically unrolled at compile-time for performance.'
                ' So the compilation time could be extremely long if the matrix size is too big.'
                ' You may use a field to store a large matrix like this, e.g.:\n'
                f'    x = ti.field(ti.f32, ({self.n}, {self.m})).\n'
                ' See https://docs.taichi.graphics/lang/articles/basic/field#matrix-size'
                ' for more details.',
                UserWarning,
                stacklevel=2)

    def element_wise_binary(self, foo, other):
        other = self.broadcast_copy(other)
        return Matrix([[foo(self(i, j), other(i, j)) for j in range(self.m)]
                       for i in range(self.n)])

    def broadcast_copy(self, other):
        if isinstance(other, (list, tuple)):
            other = Matrix(other)
        if not isinstance(other, Matrix):
            other = Matrix([[other for _ in range(self.m)]
                            for _ in range(self.n)])
        assert self.m == other.m and self.n == other.n, f"Dimension mismatch between shapes ({self.n}, {self.m}), ({other.n}, {other.m})"
        return other

    def element_wise_ternary(self, foo, other, extra):
        other = self.broadcast_copy(other)
        extra = self.broadcast_copy(extra)
        return Matrix([[
            foo(self(i, j), other(i, j), extra(i, j)) for j in range(self.m)
        ] for i in range(self.n)])

    def element_wise_writeback_binary(self, foo, other):
        if foo.__name__ == 'assign' and not isinstance(other,
                                                       (list, tuple, Matrix)):
            raise TaichiSyntaxError(
                'cannot assign scalar expr to '
                f'taichi class {type(self)}, maybe you want to use `a.fill(b)` instead?'
            )
        other = self.broadcast_copy(other)
        entries = [[foo(self(i, j), other(i, j)) for j in range(self.m)]
                   for i in range(self.n)]
        return self if foo.__name__ == 'assign' else Matrix(entries)

    def element_wise_unary(self, foo):
        return Matrix([[foo(self(i, j)) for j in range(self.m)]
                       for i in range(self.n)])

    def __matmul__(self, other):
        """Matrix-matrix or matrix-vector multiply.

        Args:
            other (Union[Matrix, Vector]): a matrix or a vector.

        Returns:
            The matrix-matrix product or matrix-vector product.

        """
        assert isinstance(other, Matrix), "rhs of `@` is not a matrix / vector"
        assert self.m == other.n, f"Dimension mismatch between shapes ({self.n}, {self.m}), ({other.n}, {other.m})"
        entries = []
        for i in range(self.n):
            entries.append([])
            for j in range(other.m):
                acc = self(i, 0) * other(0, j)
                for k in range(1, other.n):
                    acc = acc + self(i, k) * other(k, j)
                entries[i].append(acc)
        return Matrix(entries)

    def linearize_entry_id(self, *args):
        assert 1 <= len(args) <= 2
        if len(args) == 1 and isinstance(args[0], (list, tuple)):
            args = args[0]
        if len(args) == 1:
            args = args + (0, )
        # TODO(#1004): See if it's possible to support indexing at runtime
        for i, a in enumerate(args):
            if not isinstance(a, int):
                raise TaichiSyntaxError(
                    f'The {i}-th index of a Matrix/Vector must be a compile-time constant '
                    f'integer, got {type(a)}.\n'
                    'This is because matrix operations will be **unrolled** at compile-time '
                    'for performance reason.\n'
                    'If you want to *iterate through matrix elements*, use a static range:\n'
                    '  for i in ti.static(range(3)):\n'
                    '    print(i, "-th component is", vec[i])\n'
                    'See https://docs.taichi.graphics/lang/articles/advanced/meta#when-to-use-for-loops-with-tistatic for more details.'
                )
        assert 0 <= args[0] < self.n, \
            f"The 0-th matrix index is out of range: 0 <= {args[0]} < {self.n}"
        assert 0 <= args[1] < self.m, \
            f"The 1-th matrix index is out of range: 0 <= {args[1]} < {self.m}"
        return args[0] * self.m + args[1]

    def __call__(self, *args, **kwargs):
        assert kwargs == {}
        ret = self.entries[self.linearize_entry_id(*args)]
        if isinstance(ret, SNodeHostAccess):
            ret = ret.accessor.getter(*ret.key)
        elif isinstance(ret, NdarrayHostAccess):
            ret = ret.getter()
        return ret

    def set_entry(self, i, j, e):
        idx = self.linearize_entry_id(i, j)
        if impl.inside_kernel():
            self.entries[idx].assign(e)
        else:
            if isinstance(self.entries[idx], SNodeHostAccess):
                self.entries[idx].accessor.setter(e, *self.entries[idx].key)
            elif isinstance(self.entries[idx], NdarrayHostAccess):
                self.entries[idx].setter(e)
            else:
                self.entries[idx] = e

    @taichi_scope
    def subscript(self, *indices):
        assert len(indices) in [1, 2]
        i = indices[0]
        j = 0 if len(indices) == 1 else indices[1]

        if self.any_array_access:
            return self.any_array_access.subscript(i, j)
        if self.local_tensor_proxy is not None:
            assert self.dynamic_index_stride is not None
            if len(indices) == 1:
                return impl.make_tensor_element_expr(self.local_tensor_proxy,
                                                     (i, ), (self.n, ),
                                                     self.dynamic_index_stride)
            return impl.make_tensor_element_expr(self.local_tensor_proxy,
                                                 (i, j), (self.n, self.m),
                                                 self.dynamic_index_stride)
        if impl.current_cfg().dynamic_index and isinstance(
                self,
                _MatrixFieldElement) and self.dynamic_index_stride is not None:
            return impl.make_tensor_element_expr(self.entries[0].ptr, (i, j),
                                                 (self.n, self.m),
                                                 self.dynamic_index_stride)
        return self(i, j)

    @property
    def x(self):
        """Get the first element of a matrix."""
        if impl.inside_kernel():
            return self.subscript(0)
        return self[0]

    @property
    def y(self):
        """Get the second element of a matrix."""
        if impl.inside_kernel():
            return self.subscript(1)
        return self[1]

    @property
    def z(self):
        """Get the third element of a matrix."""
        if impl.inside_kernel():
            return self.subscript(2)
        return self[2]

    @property
    def w(self):
        """Get the fourth element of a matrix."""
        if impl.inside_kernel():
            return self.subscript(3)
        return self[3]

    # since Taichi-scope use v.x.assign() instead
    @x.setter
    @python_scope
    def x(self, value):
        self[0] = value

    @y.setter
    @python_scope
    def y(self, value):
        self[1] = value

    @z.setter
    @python_scope
    def z(self, value):
        self[2] = value

    @w.setter
    @python_scope
    def w(self, value):
        self[3] = value

    @property
    @python_scope
    def value(self):
        return Matrix(self.to_list())

    def to_list(self):
        return [[self(i, j) for j in range(self.m)] for i in range(self.n)]

    # host access & python scope operation
    @python_scope
    def __getitem__(self, indices):
        """Access to the element at the given indices in a matrix.

        Args:
            indices (Sequence[Expr]): the indices of the element.

        Returns:
            The value of the element at a specific position of a matrix.

        """
        if not isinstance(indices, (list, tuple)):
            indices = [indices]
        assert len(indices) in [1, 2]
        i = indices[0]
        j = 0 if len(indices) == 1 else indices[1]
        return self(i, j)

    @python_scope
    def __setitem__(self, indices, item):
        """Set the element value at the given indices in a matrix.

        Args:
            indices (Sequence[Expr]): the indices of a element.

        """
        if not isinstance(indices, (list, tuple)):
            indices = [indices]
        assert len(indices) in [1, 2]
        i = indices[0]
        j = 0 if len(indices) == 1 else indices[1]
        self.set_entry(i, j, item)

    def __len__(self):
        """Get the length of each row of a matrix"""
        return self.n

    def __iter__(self):
        if self.m == 1:
            return (self(i) for i in range(self.n))
        return ([self(i, j) for j in range(self.m)] for i in range(self.n))

    @python_scope
    def set_entries(self, value):
        if not isinstance(value, (list, tuple)):
            value = list(value)
        if not isinstance(value[0], (list, tuple)):
            value = [[i] for i in value]
        for i in range(self.n):
            for j in range(self.m):
                self[i, j] = value[i][j]

    @taichi_scope
    def cast(self, dtype):
        """Cast the matrix element data type.

        Args:
            dtype (DataType): the data type of the casted matrix element.

        Returns:
            A new matrix with each element's type is dtype.

        """
        return Matrix(
            [[ops_mod.cast(self(i, j), dtype) for j in range(self.m)]
             for i in range(self.n)])

    def trace(self):
        """The sum of a matrix diagonal elements.

        Returns:
            The sum of a matrix diagonal elements.

        """
        assert self.n == self.m
        _sum = self(0, 0)
        for i in range(1, self.n):
            _sum = _sum + self(i, i)
        return _sum

    @taichi_scope
    def inverse(self):
        """The inverse of a matrix.

        Note:
            The matrix dimension should be less than or equal to 4.

        Returns:
            The inverse of a matrix.

        Raises:
            Exception: Inversions of matrices with sizes >= 5 are not supported.

        """
        assert self.n == self.m, 'Only square matrices are invertible'
        if self.n == 1:
            return Matrix([1 / self(0, 0)])
        if self.n == 2:
            inv_determinant = impl.expr_init(1.0 / self.determinant())
            return inv_determinant * Matrix([[self(
                1, 1), -self(0, 1)], [-self(1, 0), self(0, 0)]])
        if self.n == 3:
            n = 3
            inv_determinant = impl.expr_init(1.0 / self.determinant())
            entries = [[0] * n for _ in range(n)]

            def E(x, y):
                return self(x % n, y % n)

            for i in range(n):
                for j in range(n):
                    entries[j][i] = inv_determinant * (
                        E(i + 1, j + 1) * E(i + 2, j + 2) -
                        E(i + 2, j + 1) * E(i + 1, j + 2))
            return Matrix(entries)
        if self.n == 4:
            n = 4
            inv_determinant = impl.expr_init(1.0 / self.determinant())
            entries = [[0] * n for _ in range(n)]

            def E(x, y):
                return self(x % n, y % n)

            for i in range(n):
                for j in range(n):
                    entries[j][i] = inv_determinant * (-1)**(i + j) * ((
                        E(i + 1, j + 1) *
                        (E(i + 2, j + 2) * E(i + 3, j + 3) -
                         E(i + 3, j + 2) * E(i + 2, j + 3)) - E(i + 2, j + 1) *
                        (E(i + 1, j + 2) * E(i + 3, j + 3) -
                         E(i + 3, j + 2) * E(i + 1, j + 3)) + E(i + 3, j + 1) *
                        (E(i + 1, j + 2) * E(i + 2, j + 3) -
                         E(i + 2, j + 2) * E(i + 1, j + 3))))
            return Matrix(entries)
        raise Exception(
            "Inversions of matrices with sizes >= 5 are not supported")

    def normalized(self, eps=0):
        """Normalize a vector.

        Args:
            eps (Number): a safe-guard value for sqrt, usually 0.

        Examples::

            a = ti.Vector([3, 4])
            a.normalized() # [3 / 5, 4 / 5]
            # `a.normalized()` is equivalent to `a / a.norm()`.

        Note:
            Only vector normalization is supported.

        """
        impl.static(
            impl.static_assert(self.m == 1,
                               "normalized() only works on vector"))
        invlen = 1 / (self.norm() + eps)
        return invlen * self

    def transpose(self):
        """Get the transpose of a matrix.

        Returns:
            Get the transpose of a matrix.

        """
        from taichi._funcs import _matrix_transpose  # pylint: disable=C0415
        return _matrix_transpose(self)

    @taichi_scope
    def determinant(a):
        """Get the determinant of a matrix.

        Note:
            The matrix dimension should be less than or equal to 4.

        Returns:
            The determinant of a matrix.

        Raises:
            Exception: Determinants of matrices with sizes >= 5 are not supported.

        """
        if a.n == 2 and a.m == 2:
            return a(0, 0) * a(1, 1) - a(0, 1) * a(1, 0)
        if a.n == 3 and a.m == 3:
            return a(0, 0) * (a(1, 1) * a(2, 2) - a(2, 1) * a(1, 2)) - a(
                1, 0) * (a(0, 1) * a(2, 2) - a(2, 1) * a(0, 2)) + a(
                    2, 0) * (a(0, 1) * a(1, 2) - a(1, 1) * a(0, 2))
        if a.n == 4 and a.m == 4:
            n = 4

            def E(x, y):
                return a(x % n, y % n)

            det = impl.expr_init(0.0)
            for i in range(4):
                det = det + (-1.0)**i * (
                    a(i, 0) *
                    (E(i + 1, 1) *
                     (E(i + 2, 2) * E(i + 3, 3) - E(i + 3, 2) * E(i + 2, 3)) -
                     E(i + 2, 1) *
                     (E(i + 1, 2) * E(i + 3, 3) - E(i + 3, 2) * E(i + 1, 3)) +
                     E(i + 3, 1) *
                     (E(i + 1, 2) * E(i + 2, 3) - E(i + 2, 2) * E(i + 1, 3))))
            return det
        raise Exception(
            "Determinants of matrices with sizes >= 5 are not supported")

    @staticmethod
    def diag(dim, val):
        """Construct a diagonal square matrix.

        Args:
            dim (int): the dimension of a square matrix.
            val (TypeVar): the diagonal element value.

        Returns:
            The constructed diagonal square matrix.

        """
        ret = Matrix(dim, dim)
        for i in range(dim):
            for j in range(dim):
                if i == j:
                    ret.set_entry(i, j, val)
                else:
                    ret.set_entry(i, j, 0 * val)
                    # TODO: need a more systematic way to create a "0" with the right type
        return ret

    def sum(self):
        """Return the sum of all elements."""
        ret = self.entries[0]
        for i in range(1, len(self.entries)):
            ret = ret + self.entries[i]
        return ret

    def norm(self, eps=0):
        """Return the square root of the sum of the absolute squares of its elements.

        Args:
            eps (Number): a safe-guard value for sqrt, usually 0.

        Examples::

            a = ti.Vector([3, 4])
            a.norm() # sqrt(3*3 + 4*4 + 0) = 5
            # `a.norm(eps)` is equivalent to `ti.sqrt(a.dot(a) + eps).`

        Return:
            The square root of the sum of the absolute squares of its elements.

        """
        return ops_mod.sqrt(self.norm_sqr() + eps)

    def norm_inv(self, eps=0):
        """Return the inverse of the matrix/vector `norm`. For `norm`: please see :func:`~taichi.lang.matrix.Matrix.norm`.

        Args:
            eps (Number): a safe-guard value for sqrt, usually 0.

        Returns:
            The inverse of the matrix/vector `norm`.

        """
        return ops_mod.rsqrt(self.norm_sqr() + eps)

    def norm_sqr(self):
        """Return the sum of the absolute squares of its elements."""
        return (self * self).sum()

    def max(self):
        """Return the maximum element value."""
        return ops_mod.max(*self.entries)

    def min(self):
        """Return the minimum element value."""
        return ops_mod.min(*self.entries)

    def any(self):
        """Test whether any element not equal zero.

        Returns:
            bool: True if any element is not equal zero, False otherwise.

        """
        ret = ops_mod.cmp_ne(self.entries[0], 0)
        for i in range(1, len(self.entries)):
            ret = ret + ops_mod.cmp_ne(self.entries[i], 0)
        return -ops_mod.cmp_lt(ret, 0)

    def all(self):
        """Test whether all element not equal zero.

        Returns:
            bool: True if all elements are not equal zero, False otherwise.

        """
        ret = ops_mod.cmp_ne(self.entries[0], 0)
        for i in range(1, len(self.entries)):
            ret = ret + ops_mod.cmp_ne(self.entries[i], 0)
        return -ops_mod.cmp_eq(ret, -len(self.entries))

    @taichi_scope
    def fill(self, val):
        """Fills the matrix with a specific value in Taichi scope.

        Args:
            val (Union[int, float]): Value to fill.
        """
        def assign_renamed(x, y):
            return ops_mod.assign(x, y)

        return self.element_wise_writeback_binary(assign_renamed, val)

    @python_scope
    def to_numpy(self, keep_dims=False):
        """Converts the Matrix to a numpy array.

        Args:
            keep_dims (bool, optional): Whether to keep the dimension after conversion.
                When keep_dims=False, the resulting numpy array should skip the matrix dims with size 1.

        Returns:
            numpy.ndarray: The result numpy array.
        """
        as_vector = self.m == 1 and not keep_dims
        shape_ext = (self.n, ) if as_vector else (self.n, self.m)
        return np.array(self.value).reshape(shape_ext)

    @taichi_scope
    def __ti_repr__(self):
        yield '['
        for i in range(self.n):
            if i:
                yield ', '
            if self.m != 1:
                yield '['
            for j in range(self.m):
                if j:
                    yield ', '
                yield self(i, j)
            if self.m != 1:
                yield ']'
        yield ']'

    def __str__(self):
        """Python scope matrix print support."""
        if impl.inside_kernel():
            '''
            It seems that when pybind11 got an type mismatch, it will try
            to invoke `repr` to show the object... e.g.:

            TypeError: make_const_expr_f32(): incompatible function arguments. The following argument types are supported:
                1. (arg0: float) -> taichi_core.Expr

            Invoked with: <Taichi 2x1 Matrix>

            So we have to make it happy with a dummy string...
            '''
            return f'<{self.n}x{self.m} ti.Matrix>'
        return str(self.to_numpy())

    def __repr__(self):
        return str(self.to_numpy())

    @staticmethod
    @taichi_scope
    def zero(dt, n, m=None):
        """Construct a Matrix filled with zeros.

        Args:
            dt (DataType): The desired data type.
            n (int): The first dimension (row) of the matrix.
            m (int, optional): The second dimension (column) of the matrix.

        Returns:
            :class:`~taichi.lang.matrix.Matrix`: A :class:`~taichi.lang.matrix.Matrix` instance filled with zeros.

        """
        if m is None:
            return Vector([ops_mod.cast(0, dt) for _ in range(n)])
        return Matrix([[ops_mod.cast(0, dt) for _ in range(m)]
                       for _ in range(n)])

    @staticmethod
    @taichi_scope
    def one(dt, n, m=None):
        """Construct a Matrix filled with ones.

        Args:
            dt (DataType): The desired data type.
            n (int): The first dimension (row) of the matrix.
            m (int, optional): The second dimension (column) of the matrix.

        Returns:
            :class:`~taichi.lang.matrix.Matrix`: A :class:`~taichi.lang.matrix.Matrix` instance filled with ones.

        """
        if m is None:
            return Vector([ops_mod.cast(1, dt) for _ in range(n)])
        return Matrix([[ops_mod.cast(1, dt) for _ in range(m)]
                       for _ in range(n)])

    @staticmethod
    @taichi_scope
    def unit(n, i, dt=None):
        """Construct an unit Vector (1-D matrix) i.e., a vector with only one entry filled with one and all other entries zeros.

        Args:
            n (int): The length of the vector.
            i (int): The index of the entry that will be filled with one.
            dt (DataType, optional): The desired data type.

        Returns:
            :class:`~taichi.lang.matrix.Matrix`: An 1-D unit :class:`~taichi.lang.matrix.Matrix` instance.

        """
        if dt is None:
            dt = int
        assert 0 <= i < n
        return Vector([ops_mod.cast(int(j == i), dt) for j in range(n)])

    @staticmethod
    @taichi_scope
    def identity(dt, n):
        """Construct an identity Matrix with shape (n, n).

        Args:
            dt (DataType): The desired data type.
            n (int): The number of rows/columns.

        Returns:
            :class:`~taichi.lang.matrix.Matrix`: A n x n identity :class:`~taichi.lang.matrix.Matrix` instance.

        """
        return Matrix([[ops_mod.cast(int(i == j), dt) for j in range(n)]
                       for i in range(n)])

    @staticmethod
    def rotation2d(alpha):
        return Matrix([[ops_mod.cos(alpha), -ops_mod.sin(alpha)],
                       [ops_mod.sin(alpha),
                        ops_mod.cos(alpha)]])

    @classmethod
    @python_scope
    def field(cls,
              n,
              m,
              dtype,
              shape=None,
              name="",
              offset=None,
              needs_grad=False,
              layout=Layout.AOS):
        """Construct a data container to hold all elements of the Matrix.

        Args:
            n (int): The desired number of rows of the Matrix.
            m (int): The desired number of columns of the Matrix.
            dtype (DataType, optional): The desired data type of the Matrix.
            shape (Union[int, tuple of int], optional): The desired shape of the Matrix.
            name (string, optional): The custom name of the field.
            offset (Union[int, tuple of int], optional): The coordinate offset of all elements in a field.
            needs_grad (bool, optional): Whether the Matrix need gradients.
            layout (Layout, optional): The field layout, i.e., Array Of Structure (AOS) or Structure Of Array (SOA).

        Returns:
            :class:`~taichi.lang.matrix.Matrix`: A :class:`~taichi.lang.matrix.Matrix` instance serves as the data container.

        """
        entries = []
        if isinstance(dtype, (list, tuple, np.ndarray)):
            # set different dtype for each element in Matrix
            # see #2135
            if m == 1:
                assert len(np.shape(dtype)) == 1 and len(
                    dtype
                ) == n, f'Please set correct dtype list for Vector. The shape of dtype list should be ({n}, ) instead of {np.shape(dtype)}'
                for i in range(n):
                    entries.append(
                        impl.create_field_member(dtype[i], name=name))
            else:
                assert len(np.shape(dtype)) == 2 and len(dtype) == n and len(
                    dtype[0]
                ) == m, f'Please set correct dtype list for Matrix. The shape of dtype list should be ({n}, {m}) instead of {np.shape(dtype)}'
                for i in range(n):
                    for j in range(m):
                        entries.append(
                            impl.create_field_member(dtype[i][j], name=name))
        else:
            for _ in range(n * m):
                entries.append(impl.create_field_member(dtype, name=name))
        entries, entries_grad = zip(*entries)
        entries, entries_grad = MatrixField(entries, n, m), MatrixField(
            entries_grad, n, m)
        entries.set_grad(entries_grad)
        impl.get_runtime().matrix_fields.append(entries)

        if shape is None:
            assert offset is None, "shape cannot be None when offset is being set"

        if shape is not None:
            if isinstance(shape, numbers.Number):
                shape = (shape, )
            if isinstance(offset, numbers.Number):
                offset = (offset, )

            if offset is not None:
                assert len(shape) == len(
                    offset
                ), f'The dimensionality of shape and offset must be the same  ({len(shape)} != {len(offset)})'

            dim = len(shape)
            if layout == Layout.SOA:
                for e in entries.get_field_members():
                    impl.root.dense(impl.index_nd(dim),
                                    shape).place(ScalarField(e), offset=offset)
                if needs_grad:
                    for e in entries_grad.get_field_members():
                        impl.root.dense(impl.index_nd(dim),
                                        shape).place(ScalarField(e),
                                                     offset=offset)
            else:
                impl.root.dense(impl.index_nd(dim), shape).place(entries,
                                                                 offset=offset)
                if needs_grad:
                    impl.root.dense(impl.index_nd(dim),
                                    shape).place(entries_grad, offset=offset)
        return entries

    @classmethod
    def _Vector_field(cls, n, dtype, *args, **kwargs):
        """ti.Vector.field"""
        return cls.field(n, 1, dtype, *args, **kwargs)

    @classmethod
    @python_scope
    def ndarray(cls, n, m, dtype, shape, layout=Layout.AOS):
        """Defines a Taichi ndarray with matrix elements.

        Args:
            n (int): Number of rows of the matrix.
            m (int): Number of columns of the matrix.
            dtype (DataType): Data type of each value.
            shape (Union[int, tuple[int]]): Shape of the ndarray.
            layout (Layout, optional): Memory layout, AOS by default.

        Example:
            The code below shows how a Taichi ndarray with matrix elements can be declared and defined::

                >>> x = ti.Matrix.ndarray(4, 5, ti.f32, shape=(16, 8))
        """
        if isinstance(shape, numbers.Number):
            shape = (shape, )
        return MatrixNdarray(n, m, dtype, shape, layout)

    @classmethod
    @python_scope
    def _Vector_ndarray(cls, n, dtype, shape, layout=Layout.AOS):
        """Defines a Taichi ndarray with vector elements.

        Args:
            n (int): Size of the vector.
            dtype (DataType): Data type of each value.
            shape (Union[int, tuple[int]]): Shape of the ndarray.
            layout (Layout, optional): Memory layout, AOS by default.

        Example:
            The code below shows how a Taichi ndarray with vector elements can be declared and defined::

                >>> x = ti.Vector.ndarray(3, ti.f32, shape=(16, 8))
        """
        if isinstance(shape, numbers.Number):
            shape = (shape, )
        return VectorNdarray(n, dtype, shape, layout)

    @staticmethod
    def rows(rows):
        """Construct a Matrix instance by concatenating Vectors/lists row by row.

        Args:
            rows (List): A list of Vector (1-D Matrix) or a list of list.

        Returns:
            :class:`~taichi.lang.matrix.Matrix`: A :class:`~taichi.lang.matrix.Matrix` instance filled with the Vectors/lists row by row.

        """
        mat = Matrix()
        mat.n = len(rows)
        if isinstance(rows[0], Matrix):
            for row in rows:
                assert row.m == 1, "Inputs must be vectors, i.e. m == 1"
                assert row.n == rows[
                    0].n, "Input vectors must share the same shape"
            mat.m = rows[0].n
            # l-value copy:
            mat.entries = [row(i) for row in rows for i in range(row.n)]
        elif isinstance(rows[0], list):
            for row in rows:
                assert len(row) == len(
                    rows[0]), "Input lists share the same shape"
            mat.m = len(rows[0])
            # l-value copy:
            mat.entries = [x for row in rows for x in row]
        else:
            raise Exception(
                "Cols/rows must be a list of lists, or a list of vectors")
        return mat

    @staticmethod
    def cols(cols):
        """Construct a Matrix instance by concatenating Vectors/lists column by column.

        Args:
            cols (List): A list of Vector (1-D Matrix) or a list of list.

        Returns:
            :class:`~taichi.lang.matrix.Matrix`: A :class:`~taichi.lang.matrix.Matrix` instance filled with the Vectors/lists column by column.

        """
        return Matrix.rows(cols).transpose()

    def __hash__(self):
        # TODO: refactor KernelTemplateMapper
        # If not, we get `unhashable type: Matrix` when
        # using matrices as template arguments.
        return id(self)

    def dot(self, other):
        """Perform the dot product with the input Vector (1-D Matrix).

        Args:
            other (:class:`~taichi.lang.matrix.Matrix`): The input Vector (1-D Matrix) to perform the dot product.

        Returns:
            DataType: The dot product result (scalar) of the two Vectors.

        """
        impl.static(
            impl.static_assert(self.m == 1, "lhs for dot is not a vector"))
        impl.static(
            impl.static_assert(other.m == 1, "rhs for dot is not a vector"))
        return (self * other).sum()

    def _cross3d(self, other):
        from taichi._funcs import _matrix_cross3d  # pylint: disable=C0415
        return _matrix_cross3d(self, other)

    def _cross2d(self, other):
        from taichi._funcs import _matrix_cross2d  # pylint: disable=C0415
        return _matrix_cross2d(self, other)

    def cross(self, other):
        """Perform the cross product with the input Vector (1-D Matrix).

        Args:
            other (:class:`~taichi.lang.matrix.Matrix`): The input Vector (1-D Matrix) to perform the cross product.

        Returns:
            :class:`~taichi.lang.matrix.Matrix`: The cross product result (1-D Matrix) of the two Vectors.

        """
        if self.n == 3 and self.m == 1 and other.n == 3 and other.m == 1:
            return self._cross3d(other)

        if self.n == 2 and self.m == 1 and other.n == 2 and other.m == 1:
            return self._cross2d(other)

        raise ValueError(
            "Cross product is only supported between pairs of 2D/3D vectors")

    def outer_product(self, other):
        """Perform the outer product with the input Vector (1-D Matrix).

        Args:
            other (:class:`~taichi.lang.matrix.Matrix`): The input Vector (1-D Matrix) to perform the outer product.

        Returns:
            :class:`~taichi.lang.matrix.Matrix`: The outer product result (Matrix) of the two Vectors.

        """
        from taichi._funcs import \
            _matrix_outer_product  # pylint: disable=C0415
        return _matrix_outer_product(self, other)


def Vector(n, dt=None, **kwargs):
    """Construct a `Vector` instance i.e. 1-D Matrix.

    Args:
        n (Union[int, list, tuple], np.ndarray): The desired number of entries of the Vector.
        dt (DataType, optional): The desired data type of the Vector.

    Returns:
        :class:`~taichi.lang.matrix.Matrix`: A Vector instance (1-D :class:`~taichi.lang.matrix.Matrix`).

    """
    return Matrix(n, 1, dt=dt, **kwargs)


Vector.field = Matrix._Vector_field
Vector.ndarray = Matrix._Vector_ndarray
Vector.zero = Matrix.zero
Vector.one = Matrix.one
Vector.dot = Matrix.dot
Vector.cross = Matrix.cross
Vector.outer_product = Matrix.outer_product
Vector.unit = Matrix.unit
Vector.normalized = Matrix.normalized


class _IntermediateMatrix(Matrix):
    """Intermediate matrix class for compiler internal use only.

    Args:
        n (int): Number of rows of the matrix.
        m (int): Number of columns of the matrix.
        entries (List[Expr]): All entries of the matrix.
    """
    def __init__(self, n, m, entries):
        assert isinstance(entries, list)
        assert n * m == len(entries), "Number of entries doesn't match n * m"
        self.n = n
        self.m = m
        self.entries = entries
        self.local_tensor_proxy = None
        self.any_array_access = None
        self.grad = None
        self.dynamic_index_stride = None


class _MatrixFieldElement(_IntermediateMatrix):
    """Matrix field element class for compiler internal use only.

    Args:
        field (MatrixField): The matrix field.
        indices (taichi_core.ExprGroup): Indices of the element.
    """
    def __init__(self, field, indices):
        super().__init__(field.n, field.m, [
            expr.Expr(ti_core.subscript(e.ptr, indices))
            for e in field.get_field_members()
        ])
        self.dynamic_index_stride = field.dynamic_index_stride


class MatrixField(Field):
    """Taichi matrix field with SNode implementation.

    Args:
        vars (List[Expr]): Field members.
        n (Int): Number of rows.
        m (Int): Number of columns.
    """
    def __init__(self, _vars, n, m):
        assert len(_vars) == n * m
        super().__init__(_vars)
        self.n = n
        self.m = m
        self.dynamic_index_stride = None

    def get_scalar_field(self, *indices):
        """Creates a ScalarField using a specific field member. Only used for quant.

        Args:
            indices (Tuple[Int]): Specified indices of the field member.

        Returns:
            ScalarField: The result ScalarField.
        """
        assert len(indices) in [1, 2]
        i = indices[0]
        j = 0 if len(indices) == 1 else indices[1]
        return ScalarField(self.vars[i * self.m + j])

    def calc_dynamic_index_stride(self):
        # Algorithm: https://github.com/taichi-dev/taichi/issues/3810
        paths = [ScalarField(var).snode.path_from_root() for var in self.vars]
        num_members = len(paths)
        if num_members == 1:
            self.dynamic_index_stride = 0
            return
        length = len(paths[0])
        if any(
                len(path) != length or ti_core.is_custom_type(path[length -
                                                                   1].dtype)
                for path in paths):
            return
        for i in range(length):
            if any(path[i] != paths[0][i] for path in paths):
                depth_below_lca = i
                break
        for i in range(depth_below_lca, length - 1):
            if any(path[i].ptr.type != ti_core.SNodeType.dense
                   or path[i].cell_size_bytes != paths[0][i].cell_size_bytes
                   or path[i + 1].offset_bytes_in_parent_cell != paths[0][
                       i + 1].offset_bytes_in_parent_cell for path in paths):
                return
        stride = paths[1][depth_below_lca].offset_bytes_in_parent_cell - \
                 paths[0][depth_below_lca].offset_bytes_in_parent_cell
        for i in range(2, num_members):
            if stride != paths[i][depth_below_lca].offset_bytes_in_parent_cell \
                    - paths[i - 1][depth_below_lca].offset_bytes_in_parent_cell:
                return
        self.dynamic_index_stride = stride

    @python_scope
    def fill(self, val):
        """Fills `self` with specific values.

        Args:
            val (Union[Number, List, Tuple, Matrix]): Values to fill, which should have dimension consistent with `self`.
        """
        if isinstance(val, numbers.Number):
            val = tuple(
                [tuple([val for _ in range(self.m)]) for _ in range(self.n)])
        elif isinstance(val,
                        (list, tuple)) and isinstance(val[0], numbers.Number):
            assert self.m == 1
            val = tuple([(v, ) for v in val])
        elif isinstance(val, Matrix):
            val_tuple = []
            for i in range(val.n):
                row = []
                for j in range(val.m):
                    row.append(val(i, j))
                row = tuple(row)
                val_tuple.append(row)
            val = tuple(val_tuple)
        assert len(val) == self.n
        assert len(val[0]) == self.m
        from taichi._kernels import fill_matrix  # pylint: disable=C0415
        fill_matrix(self, val)

    @python_scope
    def to_numpy(self, keep_dims=False, dtype=None):
        """Converts the field instance to a NumPy array.

        Args:
            keep_dims (bool, optional): Whether to keep the dimension after conversion.
                When keep_dims=True, on an n-D matrix field, the numpy array always has n+2 dims, even for 1x1, 1xn, nx1 matrix fields.
                When keep_dims=False, the resulting numpy array should skip the matrix dims with size 1.
                For example, a 4x1 or 1x4 matrix field with 5x6x7 elements results in an array of shape 5x6x7x4.
            dtype (DataType, optional): The desired data type of returned numpy array.

        Returns:
            numpy.ndarray: The result NumPy array.
        """
        if dtype is None:
            dtype = to_numpy_type(self.dtype)
        as_vector = self.m == 1 and not keep_dims
        shape_ext = (self.n, ) if as_vector else (self.n, self.m)
        arr = np.zeros(self.shape + shape_ext, dtype=dtype)
        from taichi._kernels import matrix_to_ext_arr  # pylint: disable=C0415
        matrix_to_ext_arr(self, arr, as_vector)
        runtime_ops.sync()
        return arr

    def to_torch(self, device=None, keep_dims=False):
        """Converts the field instance to a PyTorch tensor.

        Args:
            device (torch.device, optional): The desired device of returned tensor.
            keep_dims (bool, optional): Whether to keep the dimension after conversion.
                See :meth:`~taichi.lang.field.MatrixField.to_numpy` for more detailed explanation.

        Returns:
            torch.tensor: The result torch tensor.
        """
        import torch  # pylint: disable=C0415
        as_vector = self.m == 1 and not keep_dims
        shape_ext = (self.n, ) if as_vector else (self.n, self.m)
        # pylint: disable=E1101
        arr = torch.empty(self.shape + shape_ext,
                          dtype=to_pytorch_type(self.dtype),
                          device=device)
        from taichi._kernels import matrix_to_ext_arr  # pylint: disable=C0415
        matrix_to_ext_arr(self, arr, as_vector)
        runtime_ops.sync()
        return arr

    @python_scope
    def from_numpy(self, arr):
        if len(arr.shape) == len(self.shape) + 1:
            as_vector = True
            assert self.m == 1, "This is not a vector field"
        else:
            as_vector = False
            assert len(arr.shape) == len(self.shape) + 2
        dim_ext = 1 if as_vector else 2
        assert len(arr.shape) == len(self.shape) + dim_ext
        from taichi._kernels import ext_arr_to_matrix  # pylint: disable=C0415
        ext_arr_to_matrix(arr, self, as_vector)
        runtime_ops.sync()

    @python_scope
    def __setitem__(self, key, value):
        self.initialize_host_accessors()
        self[key].set_entries(value)

    @python_scope
    def __getitem__(self, key):
        self.initialize_host_accessors()
        key = self.pad_key(key)
        host_access = self.host_access(key)
        return Matrix([[host_access[i * self.m + j] for j in range(self.m)]
                       for i in range(self.n)])

    def __repr__(self):
        # make interactive shell happy, prevent materialization
        return f'<{self.n}x{self.m} ti.Matrix.field>'


class MatrixType(CompoundType):
    def __init__(self, n, m, dtype):
        self.n = n
        self.m = m
        self.dtype = cook_dtype(dtype)

    def __call__(self, *args):
        if len(args) == 0:
            raise TaichiSyntaxError(
                "Custom type instances need to be created with an initial value."
            )
        elif len(args) == 1:
            # fill a single scalar
            if isinstance(args[0], (numbers.Number, expr.Expr)):
                return self.filled_with_scalar(args[0])
            # fill a single vector or matrix
            entries = args[0]
        else:
            # fill in a concatenation of scalars/vectors/matrices
            entries = []
            for x in args:
                if isinstance(x, (list, tuple)):
                    entries += x
                elif isinstance(x, Matrix):
                    entries += x.entries
                else:
                    entries.append(x)
        # convert vector to nx1 matrix
        if isinstance(entries[0], numbers.Number):
            entries = [[e] for e in entries]
        # type cast
        mat = self.cast(Matrix(entries, dt=self.dtype))
        return mat

    def cast(self, mat):
        # sanity check shape
        if self.m != mat.m or self.n != mat.n:
            raise TaichiSyntaxError(
                f"Incompatible arguments for the custom vector/matrix type: ({self.n}, {self.m}), ({mat.n}, {mat.m})"
            )
        if in_python_scope():
            return Matrix([[
                int(mat(i, j)) if self.dtype in primitive_types.integer_types
                else float(mat(i, j)) for j in range(self.m)
            ] for i in range(self.n)])
        return mat.cast(self.dtype)

    def filled_with_scalar(self, value):
        return Matrix([[value for _ in range(self.m)] for _ in range(self.n)])

    def field(self, **kwargs):
        return Matrix.field(self.n, self.m, dtype=self.dtype, **kwargs)


class MatrixNdarray(Ndarray):
    """Taichi ndarray with matrix elements.

    Args:
        n (int): Number of rows of the matrix.
        m (int): Number of columns of the matrix.
        dtype (DataType): Data type of each value.
        shape (Union[int, tuple[int]]): Shape of the ndarray.
        layout (Layout): Memory layout.
    """
    def __init__(self, n, m, dtype, shape, layout):
        self.layout = layout
        self.shape = shape
        self.n = n
        self.m = m
        arr_shape = (n, m) + shape if layout == Layout.SOA else shape + (n, m)
        super().__init__(dtype, arr_shape)

    @property
    def element_shape(self):
        arr_shape = tuple(self.arr.shape)
        return arr_shape[:2] if self.layout == Layout.SOA else arr_shape[-2:]

    @python_scope
    def __setitem__(self, key, value):
        if not isinstance(value, (list, tuple)):
            value = list(value)
        if not isinstance(value[0], (list, tuple)):
            value = [[i] for i in value]
        for i in range(self.n):
            for j in range(self.m):
                self[key][i, j] = value[i][j]

    @python_scope
    def __getitem__(self, key):
        key = () if key is None else (
            key, ) if isinstance(key, numbers.Number) else tuple(key)
        return Matrix(
            [[NdarrayHostAccess(self, key, (i, j)) for j in range(self.m)]
             for i in range(self.n)])

    @python_scope
    def to_numpy(self):
        return self.ndarray_matrix_to_numpy(as_vector=0)

    @python_scope
    def from_numpy(self, arr):
        self.ndarray_matrix_from_numpy(arr, as_vector=0)

    def __deepcopy__(self, memo=None):
        ret_arr = MatrixNdarray(self.n, self.m, self.dtype, self.shape,
                                self.layout)
        ret_arr.copy_from(self)
        return ret_arr

    def _fill_by_kernel(self, val):
        from taichi._kernels import \
            fill_ndarray_matrix  # pylint: disable=C0415
        fill_ndarray_matrix(self, val)

    def __repr__(self):
        return f'<{self.n}x{self.m} {self.layout} ti.Matrix.ndarray>'


class VectorNdarray(Ndarray):
    """Taichi ndarray with vector elements.

    Args:
        n (int): Size of the vector.
        dtype (DataType): Data type of each value.
        shape (Tuple[int]): Shape of the ndarray.
        layout (Layout): Memory layout.
    """
    def __init__(self, n, dtype, shape, layout):
        self.layout = layout
        self.shape = shape
        self.n = n
        arr_shape = (n, ) + shape if layout == Layout.SOA else shape + (n, )
        super().__init__(dtype, arr_shape)

    @property
    def element_shape(self):
        arr_shape = tuple(self.arr.shape)
        return arr_shape[:1] if self.layout == Layout.SOA else arr_shape[-1:]

    @python_scope
    def __setitem__(self, key, value):
        if not isinstance(value, (list, tuple)):
            value = list(value)
        for i in range(self.n):
            self[key][i] = value[i]

    @python_scope
    def __getitem__(self, key):
        key = () if key is None else (
            key, ) if isinstance(key, numbers.Number) else tuple(key)
        return Vector(
            [NdarrayHostAccess(self, key, (i, )) for i in range(self.n)])

    @python_scope
    def to_numpy(self):
        return self.ndarray_matrix_to_numpy(as_vector=1)

    @python_scope
    def from_numpy(self, arr):
        self.ndarray_matrix_from_numpy(arr, as_vector=1)

    def __deepcopy__(self, memo=None):
        ret_arr = VectorNdarray(self.n, self.dtype, self.shape, self.layout)
        ret_arr.copy_from(self)
        return ret_arr

    def _fill_by_kernel(self, val):
        from taichi._kernels import \
            fill_ndarray_matrix  # pylint: disable=C0415
        fill_ndarray_matrix(self, val)

    def __repr__(self):
        return f'<{self.n} {self.layout} ti.Vector.ndarray>'


__all__ = ["Matrix", "Vector", "MatrixField", "MatrixNdarray", "VectorNdarray"]
