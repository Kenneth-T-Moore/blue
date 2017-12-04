"""Define the RegularGridInterpComp class."""

from __future__ import division, print_function, absolute_import

try:
    from scipy.interpolate._bsplines import make_interp_spline
except ImportError:
    from scipy import __version__ as scipy_version
    msg = "'RegularGridInterpComp' requires scipy>=0.19, but the currently" \
          " installed version is %s." % scipy_version
    raise ImportError(msg)

from scipy.interpolate.interpnd import _ndim_coords_from_arrays
import numpy as np

from openmdao.api import ExplicitComponent


class RegularGridInterpolator(object):
    """
    Interpolation on a regular grid in arbitrary dimensions.

    The data must be defined on a regular grid; the grid spacing however may be
    uneven. First, third and fifth order spline
    interpolation are supported. After setting up the interpolator object, the
    interpolation method (*slinear*, *cubic*, and
    *quintic*) may be chosen at each evaluation. Additionally, gradients are
    provided for the spline interpolation methods.

    Parameters
    ----------
    points : tuple of ndarray of float, with shapes (m1, ), ..., (mn, )
        The points defining the regular grid in n dimensions.

    values : array_like, shape (m1, ..., mn, ...)
        The data on the regular grid in n dimensions.

    method : str, optional
        The method of interpolation to perform. Supported are 'slinear',
        'cubic',  and 'quintic'. This parameter will become
        the default for the object's
        ``__call__`` method. Default is "linear".

    bounds_error : bool, optional
        If True, when interpolated values are requested outside of the
        domain of the input data, a ValueError is raised.
        If False, then `fill_value` is used.
        Default is True (raise an exception).

    fill_value : number, optional
        If provided, the value to use for points outside of the
        interpolation domain. If None, values outside
        the domain are extrapolated. Note that gradient values will always be
        extrapolated rather than set to the fill_value if bounds_error=False
        for any points outside of the interpolation domain.
        Default is `np.nan`.

    spline_dim_error : bool, optional
        If spline_dim_error=True and an order `k` spline interpolation method
        is used, then if any dimension has fewer points than `k` + 1, an error
        will be raised. If spline_dim_error=False, then the spline interpolant
        order will be reduced as needed on a per-dimension basis. Default
        is True (raise an exception).

    Methods
    -------
    __call__
    gradient
    methods

    """

    @staticmethod
    def _interp_methods():
        """Method-specific settings for interpolation and for testing."""
        interpolator_configs = {
            "slinear": 1,
            "cubic": 3,
            "quintic": 5,
        }

        spline_interps = interpolator_configs.keys()
        all_methods = list(spline_interps)

        return spline_interps, all_methods, interpolator_configs

    @staticmethod
    def methods():
        """Return a list of valid interpolation method names."""
        return ['slinear', 'cubic', 'quintic']

    def __init__(self, points, values, method="slinear", bounds_error=True,
                 fill_value=np.nan, spline_dim_error=True):
        """Initialize instance of interpolation class."""
        configs = RegularGridInterpolator._interp_methods()
        self._spline_methods, self._all_methods, self._interp_config = configs
        if method not in self._all_methods:
            all_m = ', '.join(['"' + m + '"' for m in self._all_methods])
            raise ValueError('Method "%s" is not defined. Valid methods are '
                             '%s.' % (method, all_m))
        self.method = method
        self.bounds_error = bounds_error

        if not hasattr(values, 'ndim'):
            # allow reasonable duck-typed values
            values = np.asarray(values)

        if len(points) > values.ndim:
            raise ValueError("There are %d point arrays, but values has %d "
                             "dimensions" % (len(points), values.ndim))

        if hasattr(values, 'dtype') and hasattr(values, 'astype'):
            if not np.issubdtype(values.dtype, np.inexact):
                values = values.astype(float)

        self.fill_value = fill_value
        if fill_value is not None:
            fill_value_dtype = np.asarray(fill_value).dtype
            if (hasattr(values, 'dtype') and not
                    np.can_cast(fill_value_dtype, values.dtype,
                                casting='same_kind')):
                raise ValueError("fill_value must be either 'None' or "
                                 "of a type compatible with values")

        self._ki = []
        for i, p in enumerate(points):
            n_p = len(p)
            if not np.all(np.diff(p) > 0.):
                raise ValueError("The points in dimension %d must be strictly "
                                 "ascending" % i)
            if not np.asarray(p).ndim == 1:
                raise ValueError("The points in dimension %d must be "
                                 "1-dimensional" % i)
            if not values.shape[i] == n_p:
                raise ValueError("There are %d points and %d values in "
                                 "dimension %d" % (len(p), values.shape[i], i))

            k = self._interp_config[method]
            self._ki.append(k)
            if n_p <= k:
                if not spline_dim_error:
                    self._ki[-1] = n_p - 1
                else:
                    raise ValueError("There are %d points in dimension %d,"
                                     " but method %s requires at least %d "
                                     "points per "
                                     "dimension."
                                     "" % (n_p, i, method, k + 1))

        if np.iscomplexobj(values[:]):
            raise ValueError(
                "method '%s' does not support complex values." % method)
        self.grid = tuple([np.asarray(p) for p in points])
        self.values = values
        self._xi = None
        self._all_gradients = None
        self._spline_dim_error = spline_dim_error
        self._gmethod = None

    def __call__(self, xi, method=None, compute_gradients=True):
        """
        Interpolation at coordinates.

        Parameters
        ----------
        xi : ndarray of shape (..., ndim)
            The coordinates to sample the gridded data at

        method : str, optional
            The method of interpolation to perform. Supported are 'slinear',
            'cubic', and 'quintic'. Default is None,
            which will use the method defined at
            the construction of the interpolation object instance.

        compute_gradients : bool, optional
            If a spline interpolation method is chosen, this determines
            whether gradient calculations should be made and cached.
            Default is True.
        """
        # cache latest evaluation point for gradient method's use later
        self._xi = xi

        method = self.method if method is None else method
        if method not in self._all_methods:
            all_m = ', '.join(['"' + m + '"' for m in self._all_methods])
            raise ValueError('Method "%s" is not defined. Valid methods are '
                             '%s.' % (method, all_m))

        ndim = len(self.grid)
        self.ndim = ndim
        xi = _ndim_coords_from_arrays(xi, ndim=ndim)
        if xi.shape[-1] != len(self.grid):
            raise ValueError("The requested sample points xi have dimension "
                             "%d, but this RegularGridInterpolator has "
                             "dimension %d" % (xi.shape[1], ndim))

        xi_shape = xi.shape
        xi = xi.reshape(-1, xi_shape[-1])

        if self.bounds_error:
            for i, p in enumerate(xi.T):
                if not np.logical_and(np.all(self.grid[i][0] <= p),
                                      np.all(p <= self.grid[i][-1])):
                    raise ValueError("One of the requested xi is out of bounds"
                                     " in dimension %d" % i)

        indices, norm_distances, out_of_bounds = self._find_indices(xi.T)

        if np.iscomplexobj(self.values[:]):
            raise ValueError("method '%s' does not support complex values.")
        ki = self._ki
        if method != self.method:
            # re-validate dimensions vs spline order

            ki = []
            for i, p in enumerate(self.grid):
                n_p = len(p)
                k = self._interp_config[method]
                ki.append(k)
                if n_p <= k:
                    if not self._spline_dim_error:
                        ki[-1] = n_p - 1
                    else:
                        raise ValueError("There are %d points in dimension"
                                         " %d, but method %s requires at "
                                         "least % d points per dimension."
                                         "" % (n_p, i, method, k + 1))

        interpolator = make_interp_spline
        result = self._evaluate_splines(self.values[:].T,
                                        xi,
                                        indices,
                                        interpolator,
                                        method,
                                        ki,
                                        compute_gradients=compute_gradients)

        if not self.bounds_error and self.fill_value is not None:
            result[out_of_bounds] = self.fill_value

        return result.reshape(xi_shape[:-1] +
                              self.values.shape[ndim:])

    def _evaluate_splines(self, data_values, xi, indices, interpolator, method,
                          ki, compute_gradients=True,
                          first_dim_gradient=False):
        """Inner method for separable regular grid interpolation."""
        # for spline based methods

        # requires floating point input
        xi = xi.astype(np.float)

        # ensure xi is 2D list of points to evaluate
        if xi.ndim == 1:
            xi = xi.reshape((1, xi.size))
        m, n = xi.shape

        # create container arrays for output and gradients
        result = np.empty(m)
        if compute_gradients:
            all_gradients = np.empty_like(xi)

        # Non-stationary procedure: difficult to vectorize this part entirely
        # into numpy-level operations. Unfortunately this requires explicit
        # looping over each point in xi.

        # can at least vectorize the first pass across all points in the
        # last variable of xi. This provides one dimension of the entire
        # gradient output array.
        i = n - 1
        first_values, first_derivs = self._do_spline_fit(interpolator,
                                                         self.grid[i],
                                                         data_values,
                                                         xi[:, i],
                                                         ki[i],
                                                         compute_gradients)

        if first_dim_gradient:
            top_grad = self._evaluate_splines(first_derivs,
                                              self.grid,
                                              indices,
                                              interpolator,
                                              method,
                                              ki,
                                              compute_gradients=False,
                                              first_dim_gradient=True)
            all_gradients[:, -1] = top_grad[-1]

        # the rest of the dimensions have to be on a per point-in-xi basis
        for j, x in enumerate(xi):
            gradient = np.empty_like(x)
            values = data_values[:]

            # Main process: Apply 1D interpolate in each dimension
            # sequentially, starting with the last dimension. These are then
            # "folded" into the next dimension in-place.
            for i in reversed(range(1, n)):
                if i == n - 1:
                    values = first_values[j]
                    if compute_gradients:
                        local_derivs = first_derivs[j]
                else:
                    # Interpolate and collect gradients for each 1D in this
                    # last dimensions. This collapses each 1D sequence into a
                    # scalar.
                    values, local_derivs = self._do_spline_fit(interpolator,
                                                               self.grid[i],
                                                               values,
                                                               x[i],
                                                               ki[i],
                                                               compute_gradients)

                # Chain rule: to compute gradients of the output w.r.t. xi
                # across the dimensions, apply interpolation to the collected
                # gradients. This is equivalent to multiplication by
                # dResults/dValues at each level.
                if compute_gradients:
                    gradient[i] = self._evaluate_splines(local_derivs,
                                                         x[: i],
                                                         indices,
                                                         interpolator,
                                                         method,
                                                         ki,
                                                         compute_gradients=False)

            # All values have been folded down to a single dimensional array
            # compute the final interpolated results, and gradient w.r.t. the
            # first dimension
            output_value, gradient[0] = self._do_spline_fit(interpolator,
                                                            self.grid[0],
                                                            values,
                                                            x[0],
                                                            ki[0],
                                                            compute_gradients)

            if compute_gradients:
                all_gradients[j] = gradient
            result[j] = output_value

        # Cache the computed gradients for return by the gradient method
        if compute_gradients:
            self._all_gradients = all_gradients
            # indicate what method was used to compute these
            self._gmethod = method
        return result

    def _do_spline_fit(self, interpolator, x, y, pt, k, compute_gradients):
        """Do a single interpolant call, and compute a gradient if needed."""
        interp_kwargs = {'k': k, 'axis': 0}
        local_interp = interpolator(x, y, **interp_kwargs)
        values = local_interp(pt)
        local_derivs = None
        if compute_gradients:
            local_derivs = local_interp(pt, 1)
        return values, local_derivs

    def _find_indices(self, xi):
        """Find the correct search indices for table lookups."""
        # find relevant edges between which xi are situated
        indices = []
        # compute distance to lower edge in unity units
        norm_distances = []
        # check for out of bounds xi
        out_of_bounds = np.zeros((xi.shape[1]), dtype=bool)
        # iterate through dimensions
        for x, grid in zip(xi, self.grid):
            i = np.searchsorted(grid, x) - 1
            i[i < 0] = 0
            i[i > grid.size - 2] = grid.size - 2
            indices.append(i)
            norm_distances.append((x - grid[i]) /
                                  (grid[i + 1] - grid[i]))
            if not self.bounds_error:
                out_of_bounds += x < grid[0]
                out_of_bounds += x > grid[-1]
        return indices, norm_distances, out_of_bounds

    def gradient(self, xi, method=None):
        """
        Return the computed gradients at the specified point.

        The gradients are computed as the interpolation itself is performed,
        but are cached and returned separately by this method.

        If the point for evaluation differs from the point used to produce
        the currently cached gradient, the interpolation is re-performed in
        order to return the correct gradient.

        Parameters
        ----------
        xi : ndarray of shape (..., ndim)
            The coordinates to sample the gridded data at

        method : str, optional
            The method of interpolation to perform. Supported are 'slinear',
            'cubic', and 'quintic'. Default is None, which will use the method
            defined at the construction of the interpolation object instance.

        Returns
        -------
        gradient : ndarray of shape (..., ndim)
            gradient vector of the gradients of the interpolated values with
            respect to each value in xi
        """
        # Determine if the needed gradients have been cached already
        if not method:
            method = self.method
        if method not in self._spline_methods:
            raise ValueError("method '%s' does not support gradient"
                             " calculations. " % method)

        if (self._xi is None) or \
                (not np.array_equal(xi, self._xi)) or \
                (method != self._gmethod):
            # if not, compute the interpolation to get the gradients
            self.__call__(xi, method=method)
        gradients = self._all_gradients
        gradients = gradients.reshape(np.asarray(xi).shape)
        return gradients


class RegularGridInterpComp(ExplicitComponent):
    """
    Interpolation Component generated from data on a regular grid.

    Produces smooth fits through provided training data using polynomial
    splines of order 1 (linear), 3 (cubic), or 5 (quintic). Analytic
    derivatives are automatically computed.

    For multi-dimensional data, fits are computed
    on a separable per-axis basis. If a particular dimension does not have
    enough training data points to support a selected spline order (e.g. 3
    sample points, but an order 5 quintic spline is specified) the order of the
    fitted spline with be automatically reduced for that dimension alone.

    Extrapolation is supported, but disabled by default. It can be enabled
    via initialization attribute (see below).

    Examples
    --------
    ::
        import numpy as np

        from openmdao.api import Group, Problem, IndepVarComp

        from openmdao.components.regular_grid_interp_comp import RegularGridInterpComp

        # Set up input and output data

        x = {'name' : 'x', 'values' : np.array([0.0, 1.0]), 'default' : 0, 'units' : None}

        y = {'name' : 'y', 'values' : np.array([0.0, 1.0]), 'default' : 1, 'units' : None}

        xor = {'name' : 'xor', 'values' : np.array([[0.0, 1.0], [1.0, 0.0]]),
               'default' : 1.0, 'units' : None}

        params = [x, y]

        outputs = [xor]

        # Create regular grid interpolator instance

        xor_interp = RegularGridInterpComp(params, outputs, method='slinear')

        # Set up the OpenMDAO model

        model = Group()

        ivc = IndepVarComp()

        ivc.add_output('x', 0.0)

        ivc.add_output('y', 1.0)

        model.add_subsystem('ivc', ivc, promotes=["*"])

        model.add_subsystem('comp', xor_interp, promotes=["*"])

        prob = Problem(model)

        prob.setup()

        # Now test out a 'fuzzy' XOR

        prob['x'] = 0.9

        prob['y'] = 0.001242

        prob.run_model()

        print(prob['xor'])

    Parameters
    ----------
    param_data : list of dict objects
        Training data and other attributes for the model's input parameters.
        It is a list of dictionary objects, with each dictionary containing
        information for an individual parameter. The order that these dictionaries are
        given sets the expected shape of the output training data (see
        `output_data` below).

        The relevant fields for these dictionaries are:

        - "name" : string; Name of the input parameter
        - "values" : 1D numpy array or list; sample points for the input parameter
        - "default" : float; the default value for the input parameter
        - "units" : string or NoneType; physical units for the input parameter

        Upon instantiation, each of these input parameters are automatically
            added as inputs to the component under the provided name.
    output_data : list of dict objects
        Training data and other attributes for the model's outputs.
        It is a list of dictionary objects, with each dictionary containing
        information for an individual output. The relevant fields for these
        dictionaries are:

        - "name" : string; Name of the output
        - "values" : numpy array or list; training data for the output. The
        dimension of this array must match the order and dimension of the list
        of parameters given in the `param_data` attribute. E.g., if 3 parameters
        are given with 5, 10, and 12 sample points respectively, than each
        of the `values` arrays in the dictionaries of `output_data` must
        identically have shape 5x10x12.

        - "default" : float; the default value for the output
        - "units" : string or NoneType; physical units for the output

        Upon instantiation, each of these model outputs are automatically
            added as outputs to the component under the provided name.
    method : str
        Interpolation order of the fitting spline polynomials. Supported are
        'slinear', 'cubic',  and 'quintic'. Default is "cubic".
    training_data_gradients : bool
        Sets whether gradients of the model's outputs are computed with
        respect to the output training data. Note that gradients of the model's
        outputs with respect to the inputs are always computed.
        If set to True, in addition to this extra gradient information being
        computed, each of the provided outputs will have a corresponding
        input added the component with the prefix "_train"; with the same shape
        as the training data. E.g. if the model has an output named "y" and
        training_data_gradients is set to True, then "y_train" will exist as
        an input parameter to the model. Default is False.
    num_nodes : int
        Sets the number of concurrent evaluation points for the interpolation
        to be computed at once during execution. Useful for transient models.
        This attribute sets the dimension of the inputs and outputs that
        are automatically added to the component upon instantiation.
        E.g., if num_nodes = 4, then each of the component's input parameters
        and outputs will be a 1D array of length 4.
        Default is 1 (single point scalar evaluation).
    """

    def __init__(self, param_data, output_data, method="cubic",
                 extrapolate=True, training_data_gradients=False, num_nodes=1):
        """Initialize the interpolation component."""
        super(RegularGridInterpComp, self).__init__()
        self.param_data = param_data
        self.output_data = output_data
        self.method = method
        self.extrapolate = extrapolate
        self.training_data_gradients = training_data_gradients
        self.pnames = []
        self.n = num_nodes

    def setup(self):
        """Set up the interpolation component within its problem instance."""
        n = self.n
        bounds_error = True
        if self.extrapolate:
            bounds_error = False

        self.params, outputs = [], []
        for pdict in self.param_data:
            name = pdict['name']
            self.pnames.append(name)
            values = pdict['values']
            default = np.array([pdict['default'] for i in range(n)])
            units = None
            if 'units' in pdict:
                units = pdict['units']
            self.params.append(np.asarray(values))
            self.add_input(name, val=default, units=units)

        self.sh = tuple([n] + [i.size for i in self.params])

        self.interps = {}
        for odict in self.output_data:
            name = odict['name']
            values = odict['values']
            default = np.array([odict['default'] for i in range(n)])
            units = None
            if 'units' in odict:
                units = odict['units']
            outputs.append(values)
            self.interps[name] = RegularGridInterpolator(self.params, values,
                                                         method=self.method,
                                                         bounds_error=bounds_error,
                                                         fill_value=None,
                                                         spline_dim_error=False)
            self.add_output(name, val=default, units=units)

            self._ki = self.interps[name]._ki

            if self.training_data_gradients:
                self.add_input("%s_train" % name, val=values, units=units)

            self.declare_partials(name, '*')

    def compute(self, inputs, outputs):
        """Perform the interpolation at run time."""
        pt = np.array([inputs[pname].flatten() for pname in self.pnames]).T
        for out_name in self.interps:
            if self.training_data_gradients:
                values = inputs["%s_train" % out_name]
                self.interps[out_name] = RegularGridInterpolator(self.params,
                                                                 values,
                                                                 method=self.method,
                                                                 bounds_error=False,
                                                                 fill_value=None,
                                                                 spline_dim_error=False)

            val = self.interps[out_name](pt)
            outputs[out_name] = val

    def compute_partials(self, inputs, partials):
        """
        Collect computed partial derivatives and return them.

        Checks if the needed derivatives are cached already based on the
        inputs vector. Refreshes the cache by re-computing the current point
        if necessary.
        """
        pt = np.array([inputs[pname].flatten() for pname in self.pnames]).T
        if self.training_data_gradients:
            dy_ddata = np.zeros(self.sh)
            for j in range(self.n):
                for i, axis in enumerate(self.params):
                    e_i = np.eye(axis.size)
                    interp = make_interp_spline(axis,
                                                e_i,
                                                k=self._ki[i],
                                                axis=0)
                    if i == 0:
                        val = interp(pt[j, i])
                    else:
                        val = np.outer(val, interp(pt[j, i]))
                dy_ddata[j] = val.reshape(self.sh[1:])

        for out_name in self.interps:
            dval = self.interps[out_name].gradient(pt).T
            for i, p in enumerate(self.pnames):
                partials[out_name, p] = np.diag(dval[i])

            if self.training_data_gradients:
                partials[out_name, "%s_train" % out_name] = dy_ddata