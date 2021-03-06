# -*- coding: utf8 -*-

"""Global function space discretization."""

from __future__ import division

__copyright__ = "Copyright (C) 2007 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""


import numpy as np
import numpy.linalg as la
import hedge.tools
import hedge.mesh
import hedge.optemplate
import hedge._internal
from pytools import memoize_method


class OpTemplateFunction:
    def __init__(self, discr, pp_optemplate):
        self.discr = discr
        self.pp_optemplate = pp_optemplate

    def __call__(self, **vars):
        return self.discr.run_preprocessed_optemplate(self.pp_optemplate, vars)


class _PointEvaluator(object):
    def __init__(self, discr, el_range, interp_coeff):
        self.discr = discr
        self.el_range = el_range
        self.interp_coeff = interp_coeff

    def __call__(self, field):
        from hedge.tools import log_shape
        ls = log_shape(field)
        if ls != ():
            result = np.zeros(ls, dtype=self.discr.default_scalar_type)
            from pytools import indices_in_shape
            for i in indices_in_shape(ls):
                result[i] = np.dot(
                        self.interp_coeff, field[i][self.el_range])
            return result
        else:
            return np.dot(self.interp_coeff, field[self.el_range])


# {{{ timestep calculator (deprecated)

class TimestepCalculator(object):
    def dt_factor(self, max_system_ev, order=1,
            stepper_class=None, *stepper_args):
        u"""Calculate the largest stable timestep, given a time stepper
        `stepper_class`. If none is given, RK4 is assumed.
        """

        # Calculating the correct timestep Δt for a DG scheme using the RK4
        # method is described in: "Nodal DG Methods, Algorithm, Analysis and
        # Applications" by J.S. Hesthaven & T. Warburton, p. 93, "Discrete
        # stability and timestep choise".  The implementation of timestep
        # calculation here is based upon this chapter.
        #
        # For a spatially continuous problem, the timestep can be calculated by
        # the following relation:
        #
        #           max|λop| * Δt =  C_TimeStepper,
        #
        # where max|λop| is the maximum eigenvalue of the operator and
        # C_TimeStepper represents the maximum size of the stability region of
        # the timestepper along the imaginary axis.
        #
        # For a DG-discretized problem another factor has to be added:
        #
        #            fDG = fNG * fG,
        #
        # fNG: non geometric factor fG:  geometric factor
        #
        # The discrete relation is: max|λop| * Δt = fDG * C_Timestepper
        #
        # Since the LocalDiscretization.dt_non_geometric_factor() and
        # LocalDiscretization.dt_geometric_factor() implicitly scale their
        # results for an RK4 time stepper, fDG includes already C_RK4 such as
        # fDG becomes fDG_RK4 and the relation is:
        #
        #           max|λop| * Δt = fDG_RK4
        #
        # As this is only sufficient for the use of RK4 timestepper but not for
        # any other implemented approache (e.g. Adams-Bashforth) additional
        # information about the size of the stability region is required to be
        # added into the relation.
        #
        # Unifying the relation with the size of the RK4 stability region and
        # multiplying it with the size of the specific timestepper stability
        # region brings out the correct relation:
        #
        #           max|λop| * Δt = fDG_RK4 / C_RK4 * C_TimeStepper
        #
        # C_TimeStepper gets calculated by a bisection method for every kind of
        # timestepper.

        from warnings import warn
        warn("Discretization.dt_factor() is deprecated and will be removed. "
                "Use the operator's estimate_timestep() method instead.",
                stacklevel=2)

        rk4_dt = 1 / max_system_ev \
                * (self.dt_non_geometric_factor()
                * self.dt_geometric_factor()) ** order

        from hedge.timestep.stability import \
                approximate_rk4_relative_imag_stability_region
        return rk4_dt * approximate_rk4_relative_imag_stability_region(
                None, stepper_class, stepper_args)

# }}}


class Discretization(TimestepCalculator):
    r"""The global approximation space.

    Instances of this class tie together a local discretization (i.e.
    polynomials on an elemnent) into a function space on a mesh. They
    provide creation functions such as interpolating given functions,
    differential operators and flux lifting operators.

    .. attribute:: dimensions
    .. attribute:: run_context
    .. attribute:: mesh
    .. attribute:: debug

        a set of debug flags (which are strings).

    .. attribute:: default_scalar_type

        Default numpy type for :meth:`volume_zeros`
        and company.

    .. attribute:: quad_min_degrees

        A mapping from quadrature tags to the degrees to
        which the desired quadrature is supposed to be exact.

    .. attribute:: inverse_metric_derivatives

        A list of lists of full-volume vectors,
        such that the vector *inverse_metric_derivatives[xyz_axis][rst_axis]*
        gives the metric derivatives on the entire volume.

        .. math::
            \frac{d r_{\mathtt{rst\_axis}} }{d x_{\mathtt{xyz\_axis}} }
    """

    # {{{ debug flags

    @classmethod
    def all_debug_flags(cls):
        return set([
            "ilist_generation",
            "node_permutation",
            "dump_op_code",
            "dump_dataflow_graph",
            "dump_optemplate_stages",
            "help",
            ])

    @classmethod
    def noninteractive_debug_flags(cls):
        """Return all debug flags that do not entail user interaction
        (such as key presses or console output).
        """
        return set([
            "ilist_generation",
            "node_permutation",
            ])

    # }}}

    @staticmethod
    def get_local_discretization(mesh, local_discretization=None, order=None):
        if local_discretization is None and order is None:
            raise ValueError("must supply either local_discretization "
                    "or order")
        if local_discretization is not None and order is not None:
            raise ValueError("must supply only one of local_discretization "
                    "and order")
        if local_discretization is None:
            from hedge.discretization.local import GEOMETRY_TO_LDIS
            from pytools import single_valued
            ldis_class = single_valued(
                    GEOMETRY_TO_LDIS[type(el)]
                    for el in mesh.elements)
            return ldis_class(order)
        else:
            return local_discretization

    # {{{ construction / finalization
    def __init__(self, mesh, local_discretization=None,
            order=None, quad_min_degrees={},
            debug=set(), default_scalar_type=np.float64, run_context=None):
        """
        :param quad_min_degrees: A mapping from quadrature tags to the degrees to
          which the desired quadrature is supposed to be exact.
        :param debug: A set of strings indicating which debug checks should
          be activated. See validity check below for the currently defined
          set of debug flags.
        """

        self.run_context = run_context

        if not isinstance(mesh, hedge.mesh.Mesh):
            raise TypeError("mesh must be of type hedge.mesh.Mesh")

        self.mesh = mesh

        local_discretization = self.get_local_discretization(
                mesh, local_discretization, order)

        self.dimensions = local_discretization.dimensions

        debug = set(debug)
        unknown_debug_flags = debug.difference(self.all_debug_flags())
        if unknown_debug_flags and run_context.is_head_rank:
            from warnings import warn
            warn("Unrecognized debug flags specified: "
                    + ", ".join(unknown_debug_flags))
        self.debug = debug

        if "help" in self.debug:
            print "available debug flags:"
            for df in sorted(self.all_debug_flags()):
                print "    %s" % df

        self.instrumented = False

        self.quad_min_degrees = quad_min_degrees
        self.default_scalar_type = default_scalar_type

        self.exec_functions = {}

        self._build_element_groups_and_nodes(local_discretization)
        self._calculate_local_matrices()
        self._build_interior_face_groups()

    def close(self):
        pass

    # }}}

    # {{{ instrumentation -----------------------------------------------------
    def create_op_timers(self):
        self.gather_timer = self.run_context.make_timer(
                "t_gather",
                "Time spent gathering fluxes")
        self.lift_timer = self.run_context.make_timer(
                "t_lift",
                "Time spent lifting fluxes")
        self.el_local_timer = self.run_context.make_timer(
                "t_el_local",
                "Time spent applying element-local operators (without lift)")
        self.diff_timer = self.run_context.make_timer(
                "t_diff",
                "Time spent applying applying differentiation operators")
        self.vector_math_timer = self.run_context.make_timer(
                "t_vector_math",
                "Time spent doing vector math")

        return [self.gather_timer,
                self.lift_timer,
                self.el_local_timer,
                self.diff_timer,
                self.vector_math_timer]

    def add_instrumentation(self, mgr):
        from pytools.log import IntervalTimer, EventCounter

        self.gather_counter = EventCounter("n_gather",
                "Number of flux gather invocations")
        self.lift_counter = EventCounter("n_lift",
                "Number of flux lift invocations")
        self.el_local_counter = EventCounter("n_el_local_op",
                "Number of element-local operator applications (without lift)")
        self.diff_counter = EventCounter("n_diff",
                "Number of differentiation operator applications")

        self.gather_flop_counter = EventCounter("n_flops_gather",
                "Number of floating point operations in gather")
        self.lift_flop_counter = EventCounter("n_flops_lift",
                "Number of floating point operations in lift")
        self.el_local_flop_counter = EventCounter("n_flops_el_local",
                "Number of floating point operations in element-local operator "
                "(without lift)")
        self.diff_flop_counter = EventCounter("n_flops_diff",
                "Number of floating point operations in diff operator")
        self.vector_math_flop_counter = EventCounter("n_flops_vector_math",
                "Number of floating point operations in vector math")

        self.interpolant_counter = EventCounter("n_interp",
                "Number of interpolant evaluations")

        self.interpolant_timer = IntervalTimer("t_interp",
                "Time spent evaluating interpolants")

        for op in self.create_op_timers():
            mgr.add_quantity(op)

        mgr.add_quantity(self.gather_counter)
        mgr.add_quantity(self.lift_counter)
        mgr.add_quantity(self.el_local_counter)
        mgr.add_quantity(self.diff_counter)

        mgr.add_quantity(self.gather_flop_counter)
        mgr.add_quantity(self.lift_flop_counter)
        mgr.add_quantity(self.el_local_flop_counter)
        mgr.add_quantity(self.diff_flop_counter)
        mgr.add_quantity(self.vector_math_flop_counter)

        mgr.add_quantity(self.interpolant_counter)
        mgr.add_quantity(self.interpolant_timer)

        from pytools.log import time_and_count_function
        self.interpolate_volume_function = \
                time_and_count_function(
                        self.interpolate_volume_function,
                        self.interpolant_timer,
                        self.interpolant_counter)

        self.interpolate_boundary_function = \
                time_and_count_function(
                        self.interpolate_boundary_function,
                        self.interpolant_timer,
                        self.interpolant_counter)

        from pytools import single_valued
        try:
            order = single_valued(eg.local_discretization.order
                    for eg in self.element_groups)
        except ValueError:
            pass
        else:
            mgr.set_constant("dg_order", order)

        mgr.set_constant("default_type", self.default_scalar_type.__name__)
        mgr.set_constant("element_count", len(self.mesh.elements))
        mgr.set_constant("node_count", len(self.nodes))

        for f in self.all_debug_flags():
            mgr.set_constant("debug_%s" % f, f in self.debug)

        self.instrumented = True

    # }}}

    # {{{ initialization ------------------------------------------------------
    def _build_element_groups_and_nodes(self, local_discretization):
        from hedge.mesh.element import CurvedElement
        from hedge.mesh.element import SimplicialElement

        straight_elements = [el
                for el in self.mesh.elements
                if isinstance(el, SimplicialElement)]
        curved_elements = [el
                for el in self.mesh.elements
                if isinstance(el, CurvedElement)]

        self.element_groups = []

        from hedge._internal import UniformElementRanges
        if straight_elements:
            from hedge.discretization.data import StraightElementGroup

            eg = StraightElementGroup()
            self.element_groups.append(eg)

            eg.members = straight_elements
            eg.member_nrs = np.fromiter((el.id for el in eg.members),
                    dtype=np.uint32)
            eg.local_discretization = ldis = local_discretization
            eg.ranges = UniformElementRanges(
                    0,
                    len(ldis.unit_nodes()),
                    len(self.mesh.elements))
            eg.quadrature_info = {}

            nodes_per_el = ldis.node_count()
            # mem layout:
            # [....element....][...element...]
            #  |    \
            #  [node.]
            #   | | |
            #   x y z

            # while it seems convenient, nodes should not have an
            # "element number" dimension: this would break once
            # p-adaptivity is implemented
            self.nodes = np.empty(
                    (len(self.mesh.elements) * nodes_per_el, self.dimensions),
                    dtype=float, order="C")

            unit_nodes = np.empty((nodes_per_el, self.dimensions),
                    dtype=float, order="C")

            for i_node, node in enumerate(ldis.unit_nodes()):
                unit_nodes[i_node] = node

            from hedge._internal import map_element_nodes

            for el in eg.members:
                map_element_nodes(
                        self.nodes,
                        el.id * nodes_per_el * self.dimensions,
                        el.map,
                        unit_nodes,
                        self.dimensions)

            self.group_map = [(eg, i) for i in range(len(self.mesh.elements))]

        if curved_elements:
            raise NotImplementedError

    def _calculate_local_matrices(self):
        for eg in self.element_groups:
            ldis = eg.local_discretization

            mmat = eg.mass_matrix = ldis.mass_matrix()
            immat = eg.inverse_mass_matrix = ldis.inverse_mass_matrix()
            dmats = eg.differentiation_matrices = \
                    ldis.differentiation_matrices()
            eg.stiffness_matrices = \
                    [np.dot(mmat, d) for d in dmats]
            eg.stiffness_t_matrices = \
                    [np.dot(d.T, mmat.T) for d in dmats]
            eg.minv_st = \
                    [np.dot(np.dot(immat, d.T), mmat) for d in dmats]

    @memoize_method
    def volume_jacobians(self, quadrature_tag=None, kind="numpy"):
        """Return a full-volume vector of jacobians on nodal/
        quadrature grid.
        """

        if kind != "numpy":
            raise ValueError("invalid vector kind requested")

        if quadrature_tag is None:
            vol_jac = self.volume_empty(kind=kind)

            for eg in self.element_groups:
                (eg.el_array_from_volume(vol_jac).T)[:, :] = np.array([
                    abs(el.map.jacobian())
                    for el in eg.members])

            return vol_jac
        else:
            q_info = self.get_quadrature_info(quadrature_tag)

            def make_empty_quad_vol_vector():
                return np.empty(q_info.node_count, dtype=np.float64)

            vol_jac = make_empty_quad_vol_vector()

            for eg in self.element_groups:
                eg_q_info = eg.quadrature_info[quadrature_tag]
                (eg_q_info.el_array_from_volume(vol_jac).T)[:, :] \
                        = np.array([abs(el.map.jacobian()) for el in eg.members])

            return vol_jac

    @memoize_method
    def inverse_metric_derivatives(self, quadrature_tag=None, kind="numpy"):
        """Return a list of lists of full-volume vectors,
        such that the vector *result[xyz_axis][rst_axis]*
        gives the metric derivatives on the entire volume.

        .. math::
            \frac{d r_{\mathtt{rst\_axis}} }{d x_{\mathtt{xyz\_axis}} }
        """

        if quadrature_tag is None:
            result = [[
                    self.volume_empty(kind="numpy")
                    for i in range(self.dimensions)]
                    for i in range(self.dimensions)]

            for eg in self.element_groups:
                ldis = eg.local_discretization

                for xyz_coord in range(ldis.dimensions):
                    for rst_coord in range(ldis.dimensions):
                        (eg.el_array_from_volume(
                            result[xyz_coord][rst_coord]).T)[:, :] \
                                    = [el.inverse_map.matrix[rst_coord, xyz_coord]
                                            for el in eg.members]

        else:
            q_info = self.get_quadrature_info(quadrature_tag)
            result = [[
                np.empty(q_info.node_count)
                for i in range(self.dimensions)]
                for i in range(self.dimensions)]

            for eg in self.element_groups:
                ldis = eg.local_discretization
                eg_q_info = eg.quadrature_info

                for xyz_coord in range(ldis.dimensions):
                    for rst_coord in range(ldis.dimensions):
                        (eg_q_info.el_array_from_volume(
                            result[xyz_coord][rst_coord]).T)[:, :] \
                                    = [el.inverse_map.matrix[rst_coord, xyz_coord]
                                            for el in eg.members]

        return result

    @memoize_method
    def forward_metric_derivatives(self, quadrature_tag=None, kind="numpy"):
        """Return a list of lists of full-volume vectors,
        such that the vector *result[xyz_axis][rst_axis]*
        gives the metric derivatives on the entire volume.

        .. math::
            \frac{d x_{\mathtt{xyz\_axis}} }{d r_{\mathtt{rst\_axis}} }
        """

        if quadrature_tag is None:
            result = [[
                    self.volume_empty(kind="numpy")
                    for i in range(self.dimensions)]
                    for i in range(self.dimensions)]

            for eg in self.element_groups:
                ldis = eg.local_discretization

                for xyz_coord in range(ldis.dimensions):
                    for rst_coord in range(ldis.dimensions):
                        (eg.el_array_from_volume(
                            result[xyz_coord][rst_coord]).T)[:, :] \
                                    = [el.map.matrix[rst_coord, xyz_coord]
                                            for el in eg.members]

            return result
        else:
            raise NotImplementedError(
                    "forward_metric_derivatives on quadrature grids")

    def _set_face_pair_index_data(self, fg, fp, fi_l, fi_n,
            findices_l, findices_n, findices_shuffle_op_n):
        fp.int_side.face_index_list_number = fg.register_face_index_list(
                identifier=fi_l,
                generator=lambda: findices_l)
        fp.ext_side.face_index_list_number = fg.register_face_index_list(
                identifier=(fi_n, findices_shuffle_op_n),
                generator=lambda: findices_shuffle_op_n(findices_n))
        from pytools import get_write_to_map_from_permutation
        fp.ext_native_write_map = fg.register_face_index_list(
                identifier=(fi_n, findices_shuffle_op_n, "wtm"),
                generator=lambda:
                get_write_to_map_from_permutation(
                    findices_shuffle_op_n(findices_n), findices_n))

    def _set_flux_face_data(self, f, ldis, (el, fi)):
        f.element_jacobian = el.map.jacobian()
        f.face_jacobian = el.face_jacobians[fi]
        f.element_id = el.id
        f.face_id = fi
        f.order = ldis.order
        f.normal = el.face_normals[fi]

        # This approximation is shamelessly stolen from sledge.
        # There's an important caveat, however (which took me the better
        # part of a week to figure out):
        # h on both sides of an interface must be the same, otherwise
        # the penalty term will behave very oddly.
        # This unification happens below.
        f.h = abs(el.map.jacobian() / f.face_jacobian)

    def _build_interior_face_groups(self):
        from hedge.discretization.local import FaceVertexMismatch
        from hedge.discretization.data import StraightFaceGroup
        fg_type = StraightFaceGroup
        fg = fg_type(double_sided=True,
                debug="ilist_generation" in self.debug)

        all_ldis_l = []
        all_ldis_n = []

        debug_node_perm = "node_permutation" in self.debug

        # find and match node indices along faces
        for i, (local_face, neigh_face) in enumerate(self.mesh.interfaces):
            e_l, fi_l = local_face
            e_n, fi_n = neigh_face

            eslice_l, ldis_l = self.find_el_data(e_l.id)
            eslice_n, ldis_n = self.find_el_data(e_n.id)

            all_ldis_l.append(ldis_l)
            all_ldis_n.append(ldis_n)

            vertices_l = e_l.faces[fi_l]
            vertices_n = e_n.faces[fi_n]

            findices_l = ldis_l.face_indices()[fi_l]
            findices_n = ldis_n.face_indices()[fi_n]

            try:
                findices_shuffle_op_n = ldis_l.get_face_index_shuffle_to_match(
                        vertices_l, vertices_n)

            except FaceVertexMismatch:
                # This happens if vertices_l is not a permutation
                # of vertices_n. Periodicity is the only reason why
                # that would be so.

                vertices_n, periodic_axis = self.mesh.periodic_opposite_faces[
                        vertices_n]

                findices_shuffle_op_n = \
                        ldis_l.get_face_index_shuffle_to_match(
                                vertices_l, vertices_n)
            else:
                periodic_axis = None

            # create and fill the face pair
            fp = fg_type.FacePair()

            fp.int_side.el_base_index = eslice_l.start
            fp.ext_side.el_base_index = eslice_n.start

            self._set_face_pair_index_data(fg, fp, fi_l, fi_n,
                    findices_l, findices_n, findices_shuffle_op_n)

            self._set_flux_face_data(fp.int_side, ldis_l, local_face)
            self._set_flux_face_data(fp.ext_side, ldis_n, neigh_face)

            # unify h across the faces
            fp.int_side.h = fp.ext_side.h = max(fp.int_side.h, fp.ext_side.h)
            assert (abs(fp.int_side.face_jacobian - fp.ext_side.face_jacobian)
                    / abs(fp.int_side.face_jacobian)) < 1e-13

            # check that we set the C++ attrs, not new Python ones
            assert len(fp.__dict__) == 0
            assert len(fp.int_side.__dict__) == 0
            assert len(fp.ext_side.__dict__) == 0

            fg.face_pairs.append(fp)

            # check that nodes match up
            if (debug_node_perm
                    and ldis_l.has_facial_nodes
                    and ldis_n.has_facial_nodes):
                findices_shuffled_n = findices_shuffle_op_n(findices_n)

                for i, j in zip(findices_l, findices_shuffled_n):
                    dist = self.nodes[eslice_l.start + i] \
                            - self.nodes[eslice_n.start + j]
                    if periodic_axis is not None:
                        dist[periodic_axis] = 0
                    assert la.norm(dist) < 1e-14

        if len(fg.face_pairs):
            from pytools import single_valued
            ldis_l = single_valued(all_ldis_l)
            ldis_n = single_valued(all_ldis_n)

            fg.commit(self, ldis_l, ldis_n)

            self.face_groups = [fg]
        else:
            self.face_groups = []

    # }}}

    # {{{ boundary descriptors ------------------------------------------------
    def is_boundary_tag_nonempty(self, tag):
        return bool(self.mesh.tag_to_boundary.get(tag, []))

    @memoize_method
    def get_boundary(self, tag):
        """Get a Boundary instance for a given `tag'.

        If there is no boundary tagged with `tag', an empty Boundary instance
        is returned. Asking for a nonexistant boundary is not an error.
        (Otherwise get_boundary would unnecessarily become non-local when run
        in parallel.)
        """
        from hedge.discretization.data import StraightFaceGroup
        nodes = []
        vol_indices = []
        fg_type = StraightFaceGroup
        face_group = fg_type(double_sided=False,
                debug="ilist_generation" in self.debug)
        ldis = None  # if this boundary is empty, we might as well have no ldis
        el_face_to_face_group_and_face_pair = {}

        for ef in self.mesh.tag_to_boundary.get(tag, []):
            el, face_nr = ef

            el_slice, ldis = self.find_el_data(el.id)
            face_indices = ldis.face_indices()[face_nr]
            face_indices_ary = np.array(face_indices, dtype=np.intp)

            f_start = len(nodes)
            nodes.extend(self.nodes[el_slice.start + face_indices_ary])
            vol_indices.extend(el_slice.start + face_indices_ary)

            # create the face pair
            fp = face_group.FacePair()
            fp.int_side.el_base_index = el_slice.start
            fp.ext_side.el_base_index = f_start
            fp.int_side.face_index_list_number = face_group.register_face_index_list(
                    identifier=face_nr,
                    generator=lambda: face_indices)
            fp.ext_side.face_index_list_number = face_group.register_face_index_list(
                    identifier=(),
                    generator=lambda: tuple(xrange(len(face_indices))))
            self._set_flux_face_data(fp.int_side, ldis, ef)

            # check that all property assigns found their C++-side slots
            assert len(fp.__dict__) == 0
            assert len(fp.int_side.__dict__) == 0
            assert len(fp.ext_side.__dict__) == 0

            face_group.face_pairs.append(fp)

            # and make it possible to find it later
            el_face_to_face_group_and_face_pair[ef] = \
                    face_group, len(face_group.face_pairs)-1

        if ldis is not None:
            face_group.commit(self, ldis, ldis)
            face_groups = [face_group]
        else:
            face_groups = []

        from hedge._internal import UniformElementRanges
        fg_ranges = [UniformElementRanges(
            0,  # FIXME: need to vary element starts
            fg.ldis_loc.face_node_count(), len(face_group.face_pairs))
            for fg in face_groups]

        nodes_ary = np.array(nodes)
        nodes_ary.shape = (len(nodes), self.dimensions)

        from hedge.discretization.data import Boundary
        bdry = Boundary(
                discr=self,
                nodes=nodes_ary,
                vol_indices=vol_indices,
                face_groups=face_groups,
                fg_ranges=fg_ranges,
                el_face_to_face_group_and_face_pair=
                el_face_to_face_group_and_face_pair)

        return bdry

    # }}}

    # {{{ quadrature descriptors
    @memoize_method
    def get_quadrature_info(self, quad_tag):
        from hedge.discretization.local import FaceVertexMismatch
        from hedge.discretization.data import QuadratureInfo

        try:
            min_degree = self.quad_min_degrees[quad_tag]
        except KeyError:
            raise RuntimeError("minimum degree for quadrature tag '%s' "
                    "is undefined" % quad_tag)

        q_info = QuadratureInfo()
        q_info.node_count = 0
        q_info.int_faces_node_count = 0
        q_info.face_groups = []

        # {{{ process element groups
        for eg in self.element_groups:
            eg_q_info = eg.quadrature_info[quad_tag] = eg.QuadratureInfo(
                    eg, min_degree, q_info.node_count,
                    q_info.int_faces_node_count)

            q_info.node_count += eg_q_info.ranges.total_size
            q_info.int_faces_node_count += eg_q_info.el_faces_ranges.total_size

        # }}}

        # {{{ process face groups
        for fg in self.face_groups:
            quad_fg = type(fg)(double_sided=True,
                    debug="ilist_generation" in self.debug)
            q_info.face_groups.append(quad_fg)

            ldis_l = fg.ldis_loc
            ldis_n = fg.ldis_opp
            ldis_q_info_l = ldis_l.get_quadrature_info(
                    self.quad_min_degrees[quad_tag])
            ldis_q_info_n = ldis_n.get_quadrature_info(
                    self.quad_min_degrees[quad_tag])
            fnc_l = ldis_q_info_l.face_node_count()
            fnc_n = ldis_q_info_n.face_node_count()

            for fp in fg.face_pairs:
                e_l = self.mesh.elements[fp.int_side.element_id]
                e_n = self.mesh.elements[fp.ext_side.element_id]
                fi_l = fp.int_side.face_id
                fi_n = fp.ext_side.face_id

                vertices_l = e_l.faces[fi_l]
                vertices_n = e_n.faces[fi_n]

                try:
                    findices_shuffle_op_n = \
                            ldis_q_info_l.get_face_index_shuffle_to_match(
                                    vertices_l, vertices_n)

                except FaceVertexMismatch:
                    # This happens if vertices_l is not a permutation
                    # of vertices_n. Periodicity is the only reason why
                    # that would be so.

                    vertices_n, periodic_axis = self.mesh.periodic_opposite_faces[
                            vertices_n]

                    findices_shuffle_op_n = \
                            ldis_q_info_l.get_face_index_shuffle_to_match(
                                    vertices_l, vertices_n)

                # create and fill the face pair
                quad_fp = type(quad_fg).FacePair()

                def find_el_base_index(el):
                    group, idx = self.group_map[el.id]
                    return group.quadrature_info[quad_tag].el_faces_ranges[idx].start

                quad_fp.int_side.el_base_index = find_el_base_index(e_l)
                quad_fp.ext_side.el_base_index = find_el_base_index(e_n)

                findices_l = tuple(range(fnc_l*fi_l, fnc_l*(fi_l+1)))
                findices_n = tuple(range(fnc_n*fi_n, fnc_n*(fi_n+1)))

                self._set_face_pair_index_data(quad_fg, quad_fp, fi_l, fi_n,
                        findices_l, findices_n, findices_shuffle_op_n)

                self._set_flux_face_data(quad_fp.int_side, ldis_l, (e_l, fi_l))
                self._set_flux_face_data(quad_fp.ext_side, ldis_n, (e_n, fi_n))

                quad_fp.int_side.h = quad_fp.ext_side.h = max(
                        quad_fp.int_side.h, quad_fp.ext_side.h)

                # check that we have set the C++ attrs, not new Python ones
                assert len(quad_fp.__dict__) == 0
                assert len(quad_fp.int_side.__dict__) == 0
                assert len(quad_fp.ext_side.__dict__) == 0

                quad_fg.face_pairs.append(quad_fp)

            if len(fg.face_pairs):
                def get_write_el_base(read_base, el_id):
                    return self.find_el_range(el_id).start

                quad_fg.commit(self, ldis_l, ldis_n, get_write_el_base)

                quad_fg.ldis_loc_quad_info = ldis_q_info_l
                quad_fg.ldis_opp_quad_info = ldis_q_info_n

        # }}}

        return q_info

    # }}}

    # {{{ vector construction -------------------------------------------------
    def __len__(self):
        """Return the number of nodes in this discretization."""
        return len(self.nodes)

    def len_boundary(self, tag):
        return len(self.get_boundary(tag).nodes)

    def get_kind(self, field):
        return "numpy"

    compute_kind = "numpy"

    def convert_volume(self, field, kind, dtype=None):
        orig_kind = self.get_kind(field)

        if orig_kind != "numpy":
            raise ValueError(
                    "unable to perform kind conversion: %s -> %s"
                    % (orig_kind, kind))

        if dtype is not None:
            from hedge.tools import cast_field
            field = cast_field(field, dtype)

        return field

    def convert_boundary(self, field, tag, kind, dtype=None):
        orig_kind = self.get_kind(field)

        if orig_kind != "numpy":
            raise ValueError(
                    "unable to perform kind conversion: %s -> %s"
                    % (orig_kind, kind))

        if dtype is not None:
            from hedge.tools import cast_field
            field = cast_field(field, dtype)

        return field

    def convert_boundary_async(self, field, tag, kind, read_map=None):
        from hedge.tools.futures import ImmediateFuture

        if read_map is not None:
            from hedge.tools import log_shape
            ls = log_shape(field)
            if field.dtype == object or ls == ():
                from hedge.tools import with_object_array_or_scalar
                field = with_object_array_or_scalar(
                        lambda f: f[read_map], field)
            else:
                field = np.asarray(
                        np.take(field, read_map, axis=len(ls)),
                        order="C")

        return ImmediateFuture(
                self.convert_boundary(field, tag, kind))

    def volume_empty(self, shape=(), dtype=None, kind="numpy"):
        if kind != "numpy":
            raise ValueError("invalid vector kind requested")

        if dtype is None:
            dtype = self.default_scalar_type
        return np.empty(shape + (len(self.nodes),), dtype)

    def volume_zeros(self, shape=(), dtype=None, kind="numpy"):
        if kind != "numpy":
            raise ValueError("invalid vector kind requested")

        if dtype is None:
            dtype = self.default_scalar_type
        return np.zeros(shape + (len(self.nodes),), dtype)

    def interpolate_volume_function(self, f, dtype=None, kind=None):
        if kind is None:
            kind = self.compute_kind

        try:
            # are we interpolating many fields at once?
            shape = f.shape
        except AttributeError:
            # no, just one
            shape = ()

        slice_pfx = (slice(None),) * len(shape)
        out = self.volume_empty(shape, dtype, kind="numpy")
        for eg in self.element_groups:
            for el, el_slice in zip(eg.members, eg.ranges):
                for point_nr in xrange(el_slice.start, el_slice.stop):
                    out[slice_pfx + (point_nr,)] = \
                                f(self.nodes[point_nr], el)
        return self.convert_volume(out, kind=kind)

    def boundary_empty(self, tag, shape=(), dtype=None, kind="numpy"):
        if kind not in ["numpy", "numpy-mpi-recv"]:
            raise ValueError("invalid vector kind requested")

        if dtype is None:
            dtype = self.default_scalar_type
        return np.empty(shape + (len(self.get_boundary(tag).nodes),), dtype)

    def boundary_zeros(self, tag, shape=(), dtype=None, kind="numpy"):
        if kind not in ["numpy", "numpy-mpi-recv"]:
            raise ValueError("invalid vector kind requested")
        if dtype is None:
            dtype = self.default_scalar_type

        return np.zeros(shape + (len(self.get_boundary(tag).nodes),), dtype)

    def interpolate_boundary_function(self, f, tag, dtype=None, kind=None):
        if kind is None:
            kind = self.compute_kind

        try:
            # are we interpolating many fields at once?
            shape = f.shape
        except AttributeError:
            # no, just one
            shape = ()

        out = self.boundary_zeros(tag, shape, dtype, kind="numpy")
        slice_pfx = (slice(None),) * len(shape)
        for point_nr, x in enumerate(self.get_boundary(tag).nodes):
            out[slice_pfx + (point_nr,)] = f(x, None)  # FIXME

        return self.convert_boundary(out, tag, kind)

    @memoize_method
    def boundary_normals(self, tag, dtype=None, kind=None):
        if kind is None:
            kind = self.compute_kind

        result = self.boundary_zeros(shape=(self.dimensions,),
                tag=tag, dtype=dtype, kind="numpy")
        for fg in self.get_boundary(tag).face_groups:
            for face_pair in fg.face_pairs:
                oeb = face_pair.ext_side.el_base_index
                opp_index_list = \
                        fg.index_lists[face_pair.ext_side.face_index_list_number]
                for i in opp_index_list:
                    result[:, oeb+i] = face_pair.int_side.normal

        return self.convert_boundary(result, tag, kind)

    def volumize_boundary_field(self, bfield, tag, kind=None):
        if kind is None:
            kind = self.compute_kind

        if kind != "numpy":
            raise ValueError("invalid target vector kind in "
                    "volumize_boundary_field")

        bdry = self.get_boundary(tag)

        def f(subfld):
            result = self.volume_zeros(dtype=bfield.dtype, kind="numpy")
            result[bdry.vol_indices] = subfld
            return result

        from hedge.tools import with_object_array_or_scalar
        return with_object_array_or_scalar(f, bfield)

    def boundarize_volume_field(self, field, tag, kind=None):
        if kind is None:
            kind = self.compute_kind

        if kind != "numpy":
            raise ValueError("invalid target vector kind in "
                    "boundarize_volume_field")

        bdry = self.get_boundary(tag)

        from hedge.tools import log_shape, is_obj_array
        ls = log_shape(field)

        if is_obj_array(field):
            if len(field) == 0:
                return np.zeros(())

            dtype = None
            for field_i in field:
                try:
                    dtype = field_i.dtype
                    break
                except AttributeError:
                    pass

            result = self.boundary_empty(tag, shape=ls, dtype=dtype)
            from pytools import indices_in_shape
            for i in indices_in_shape(ls):
                field_i = field[i]
                if isinstance(field_i, np.ndarray):
                    result[i] = field_i[bdry.vol_indices]
                else:
                    # a scalar, will be broadcast
                    result[i] = field_i

            return result
        else:
            return field[tuple(slice(None) for i in range(
                len(ls))) + (bdry.vol_indices,)]

    def boundarize_volume_field_async(self, field, tag, kind=None):
        from hedge.tools.futures import ImmediateFuture
        return ImmediateFuture(
                self.boundarize_volume_field(field, tag, kind))

    def prepare_from_neighbor_map(self, indices):
        return np.array(indices, dtype=np.intp)

    # }}}

    # {{{ scalar reduction

    @memoize_method
    def _integral_op(self, arg_shape):
        import hedge.optemplate as sym
        if arg_shape == ():
            u = np.zeros(1, dtype=np.object)
            u[0] = sym.Field("arg")
        else:
            u = sym.make_sym_array("arg", arg_shape)

        return self.compile(sym.integral(u))

    def integral(self, volume_vector):
        from hedge.tools import log_shape
        return self._integral_op(log_shape(volume_vector))(arg=volume_vector)

    @memoize_method
    def mesh_volume(self):
        return self.integral(ones_on_volume(self))

    @memoize_method
    def _norm_op(self, p, arg_shape):
        import hedge.optemplate as sym
        if arg_shape == ():
            u = np.zeros(1, dtype=np.object)
            u[0] = sym.Field("arg")
        else:
            u = sym.make_sym_array("arg", arg_shape)

        return self.compile(sym.norm(p, u))

    def norm(self, volume_vector, p=2):
        from hedge.tools import log_shape
        return self._norm_op(p, log_shape(volume_vector))(arg=volume_vector)

    @memoize_method
    def _inner_product_op(self, p, arg_shape):
        import hedge.optemplate as sym
        if arg_shape == ():
            a = np.zeros(1, dtype=np.object)
            a[0] = sym.Field("a")
            b = np.zeros(1, dtype=np.object)
            b[0] = sym.Field("b")
        else:
            a = sym.make_sym_array("a", arg_shape)
            b = sym.make_sym_array("b", arg_shape)

        return np.sum(a * sym.MassOperator(b))

    def inner_product(self, a, b):
        from hedge.tools import log_shape
        shape = log_shape(a)
        if log_shape(b) != shape:
            raise ValueError("second arg of inner_product must have same shape")

        return self._inner_product_op(shape)(a=a, b=b)

    def nodewise_max(self, a):
        from warnings import warn
        warn("nodewise_max is deprecated, build an equivalent operator instead",
                DeprecationWarning)

        return np.max(a)

    # }}}

    # {{{ vector primitives

    def get_vector_primitive_factory(self):
        from hedge.vector_primitives import VectorPrimitiveFactory
        return VectorPrimitiveFactory()

    # }}}

    # {{{ element data retrieval

    def find_el_range(self, el_id):
        group, idx = self.group_map[el_id]
        return group.ranges[idx]

    def find_el_discretization(self, el_id):
        return self.group_map[el_id][0].local_discretization

    def find_el_data(self, el_id):
        group, idx = self.group_map[el_id]
        return group.ranges[idx], group.local_discretization

    def find_element(self, idx):
        for i, (start, stop) in enumerate(self.element_group):
            if start <= idx < stop:
                return i
        raise ValueError("not a valid dof index")

    # }}}

    # {{{ misc stuff

    @memoize_method
    def dt_non_geometric_factor(self):
        distinct_ldis = set(eg.local_discretization
                for eg in self.element_groups)
        return min(ldis.dt_non_geometric_factor()
                for ldis in distinct_ldis)

    @memoize_method
    def dt_geometric_factor(self):
        return min(min(eg.local_discretization.dt_geometric_factor(
            [self.mesh.points[i] for i in el.vertex_indices], el)
            for el in eg.members)
            for eg in self.element_groups)

    def get_point_evaluator(self, point, use_btree=False, thresh=0):
        def make_point_evaluator(el, eg, rng):
            """For a given element *el*/element group *eg* in which *point*
            is contained, return a callable that accepts fields an returns
            an evaluation at *point*.
            """

            ldis = eg.local_discretization
            basis_values = np.array([
                phi(el.inverse_map(point))
                for phi in ldis.basis_functions()])
            vdm_t = ldis.vandermonde().T
            return _PointEvaluator(
                    discr=self,
                    el_range=rng,
                    interp_coeff=la.solve(vdm_t, basis_values))

        if use_btree:
            elements_in_bucket = self.get_spatial_btree().generate_matches(point)
            for el, rng, eg in elements_in_bucket:
                if el.contains_point(point, thresh):
                    pe = make_point_evaluator(el, eg, rng)
                    return pe
        else:
            for eg in self.element_groups:
                for el, rng in zip(eg.members, eg.ranges):
                    if el.contains_point(point, thresh):
                        pe = make_point_evaluator(el, eg, rng)
                        return pe

        raise RuntimeError(
                "point %s not found. Consider changing threshold."
                % point)

    def get_regrid_values(self, field_in, new_discr, dtype=None,
            use_btree=True, thresh=0):
        """:param field_in: nodal values on old grid.
        :param new_discr: new discretization.
        :param use_btree: bool to decide if a spatial binary tree will be used.
        """

        if self.get_kind(field_in) != "numpy":
            raise NotImplementedError(
                    "get_regrid_values needs numpy input field")

        def regrid(scalar_field):
            result = new_discr.volume_empty(dtype=dtype, kind="numpy")
            for ii in range(len(new_discr.nodes)):
                pe = self.get_point_evaluator(new_discr.nodes[ii], use_btree, thresh)
                result[ii] = pe(scalar_field)
            return result

        from pytools.obj_array import with_object_array_or_scalar
        return with_object_array_or_scalar(regrid, field_in)

    @memoize_method
    def get_spatial_btree(self):
        from pytools.spatial_btree import SpatialBinaryTreeBucket
        spatial_btree = SpatialBinaryTreeBucket(*self.mesh.bounding_box())

        for eg in self.element_groups:
            for el, rng in zip(eg.members, eg.ranges):
                spatial_btree.insert(
                        (el, rng, eg), el.bounding_box(self.mesh.points))

        return spatial_btree

    # }}}

    # {{{ op template execution

    def compile(self, optemplate, post_bind_mapper=lambda x: x,
            type_hints={}):
        from hedge.optemplate.mappers import QuadratureUpsamplerRemover
        optemplate = QuadratureUpsamplerRemover(self.quad_min_degrees)(
                optemplate)

        ex = self.executor_class(self, optemplate, post_bind_mapper,
                type_hints)

        if "dump_dataflow_graph" in self.debug:
            ex.code.dump_dataflow_graph()

        if self.instrumented:
            ex.instrument()
        return ex

    def add_function(self, name, func):
        self.exec_functions[name] = func

    # }}}


# {{{ random utilities

class SymmetryMap(object):
    """A symmetry map on global DG functions.

    Suppose that the L{Mesh} on which a L{Discretization} is defined has
    is mapped onto itself by a nontrivial symmetry map M{f(.)}. Then
    this class allows you to carry out this map on vectors representing
    functions on this L{Discretization}.
    """
    def __init__(self, discr, sym_map, element_map, threshold=1e-13):
        self.discretization = discr

        complete_el_map = {}
        for i, j in element_map.iteritems():
            complete_el_map[i] = j
            complete_el_map[j] = i

        self.map = {}

        for eg in discr.element_groups:
            for el, el_slice in zip(eg.members, eg.ranges):
                mapped_i_el = complete_el_map[el.id]
                mapped_slice = discr.find_el_range(mapped_i_el)
                for i_pt in range(el_slice.start, el_slice.stop):
                    pt = discr.nodes[i_pt]
                    mapped_pt = sym_map(pt)
                    for m_i_pt in range(mapped_slice.start, mapped_slice.stop):
                        if (la.norm(discr.nodes[m_i_pt] - mapped_pt)
                                < threshold):
                            self.map[i_pt] = m_i_pt
                            break

                    if i_pt not in self.map:
                        for m_i_pt in range(
                                mapped_slice.start, mapped_slice.stop):
                            print la.norm_2(discr.nodes[m_i_pt] - mapped_pt)
                        raise RuntimeError("no symmetry match found")

    def __call__(self, vec):
        result = self.discretization.volume_zeros()
        for i, mapped_i in self.map.iteritems():
            result[mapped_i] = vec[i]
        return result


def generate_random_constant_on_elements(discr):
    result = discr.volume_zeros()
    import random
    for eg in discr.element_groups:
        for e_start, e_end in eg.ranges:
            result[e_start:e_end] = random.random()
    return result


def ones_on_boundary(discr, tag):
    result = discr.volume_zeros(kind="numpy")

    try:
        faces = discr.mesh.tag_to_boundary[tag]
    except KeyError:
        pass
    else:
        for face in faces:
            el, fl = face

            el_range, ldis = discr.find_el_data(el.id)
            fl_indices = ldis.face_indices()[fl]
            result[el_range.start
                    .asarray(fl_indices, dtype=np.intp)] = 1

    return result


def ones_on_volume(discr):
    result = discr.volume_empty()
    result.fill(1)
    return result

# }}}


# {{{ projection between different discretizations

class Projector:
    def __init__(self, from_discr, to_discr):
        self.from_discr = from_discr
        self.to_discr = to_discr

        self.interp_matrices = []
        for from_eg, to_eg in zip(
                from_discr.element_groups, to_discr.element_groups):
            from_ldis = from_eg.local_discretization
            to_ldis = to_eg.local_discretization

            from_count = from_ldis.node_count()
            to_count = to_ldis.node_count()

            # check that the two element groups have the same members
            for from_el, to_el in zip(from_eg.members, to_eg.members):
                assert from_el is to_el

            from hedge.tools import permutation_matrix

            # assemble the from->to mode permutation matrix, guided by
            # mode identifiers
            if to_count > from_count:
                to_node_ids_to_idx = dict(
                        (nid, i) for i, nid in
                        enumerate(to_ldis.generate_mode_identifiers()))

                to_indices = [
                    to_node_ids_to_idx[from_nid]
                    for from_nid in from_ldis.generate_mode_identifiers()]

                pmat = permutation_matrix(
                    to_indices=to_indices,
                    h=to_count, w=from_count)
            else:
                from_node_ids_to_idx = dict(
                        (nid, i) for i, nid in
                        enumerate(from_ldis.generate_mode_identifiers()))

                from_indices = [
                    from_node_ids_to_idx[to_nid]
                    for to_nid in to_ldis.generate_mode_identifiers()]

                pmat = permutation_matrix(
                    from_indices=from_indices,
                    h=to_count, w=from_count)

            # build interpolation matrix
            from_matrix = from_ldis.vandermonde()
            to_matrix = to_ldis.vandermonde()

            from hedge.tools import leftsolve
            self.interp_matrices.append(
                    np.asarray(
                        leftsolve(from_matrix, np.dot(to_matrix, pmat)),
                        order="C"))

    def __call__(self, from_vec):
        from hedge._internal import perform_elwise_operator
        from hedge.tools import log_shape

        ls = log_shape(from_vec)
        result = np.empty(shape=ls, dtype=object)

        from pytools import indices_in_shape
        for i in indices_in_shape(ls):
            result_i = self.to_discr.volume_zeros(kind="numpy")
            result[i] = result_i

            for from_eg, to_eg, imat in zip(
                    self.from_discr.element_groups,
                    self.to_discr.element_groups,
                    self.interp_matrices):
                perform_elwise_operator(
                        from_eg.ranges, to_eg.ranges,
                        imat, from_vec[i], result_i)

        if ls == ():
            return result[()]
        else:
            return result

# }}}


# {{{ filter

class ExponentialFilterResponseFunction:
    """A typical exponential-falloff mode response filter function.

    See description in Section 5.6.1 of Hesthaven/Warburton.
    """
    def __init__(self, min_amplification=0.1, order=6):
        """Construct the filter function.

        The amplification factor of the lowest-order (constant) mode is
        always 1.

        :param min_amplification: The amplification factor applied to
            the highest mode.
        :param order: The order of the filter. This controls how fast
          (or slowly) the *min_amplification* is reached.
        """
        from math import log
        self.alpha = - log(min_amplification)
        self.order = order

    def __call__(self, mode_idx, ldis):
        eta = sum(mode_idx) / ldis.order

        from math import exp
        return exp(- self.alpha * eta ** self.order)


class Filter:
    def __init__(self, discr, mode_response_func):
        from warnings import warn
        warn("hedge.discretization.Filter is deprecated. "
                "Use hedge.optemplate.FilterOperator directly.")

        from hedge.optemplate.operators import FilterOperator
        self.bound_filter_op = FilterOperator(mode_response_func).bind(discr)

    def __call__(self, f):
        return self.bound_filter_op(f)

# }}}


# {{{ high-precision projection in 1D

def adaptive_project_function_1d(discr, f, dtype=None, kind=None,
        epsrel=1e-8, epsabs=1e-8):
    if kind is None:
        kind = discr.compute_kind

    try:
        # are we interpolating many fields at once?
        shape = f.shape
    except AttributeError:
        # no, just one
        shape = ()

    from scipy.integrate import quad

    out = discr.volume_empty(shape, dtype, kind="numpy")

    from pytools import indices_in_shape
    for idx in indices_in_shape(shape):
        for eg in discr.element_groups:
            ldis = eg.local_discretization
            basis = ldis.basis_functions()

            for el, el_slice in zip(eg.members, eg.ranges):
                a = discr.nodes[el_slice.start][0]
                b = discr.nodes[el_slice.stop-1][0]
                el_result = np.dot(
                        ldis.vandermonde(),
                        el.inverse_map.jacobian()*np.array([
                            quad(func=lambda x:
                                basis_func(el.inverse_map(np.array([x])))
                                * np.asarray(f(np.array([x]), el))[idx], a=a, b=b,
                                epsrel=epsrel, epsabs=epsabs)[0]
                            for i, basis_func in enumerate(basis)]))

                out[idx + (el_slice,)] = el_result

    return discr.convert_volume(out, kind=kind)

# }}}


# vim: foldmethod=marker
