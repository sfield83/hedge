# Hedge - the Hybrid'n'Easy DG Environment
# Copyright (C) 2009 Andreas Stock
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.




from __future__ import division
import numpy
import numpy.linalg as la


def main():
    from hedge.timestep import RK4TimeStepper
    from hedge.tools import mem_checkpoint
    from math import sin, cos, pi, sqrt, tanh
    from math import floor

    from hedge.backends import guess_run_context
    rcon = guess_run_context(disable=set(["cuda"]))

    # mesh setup --------------------------------------------------------------

    dim = 2

    if rcon.is_head_rank:
        #from hedge.mesh import make_disk_mesh
        #mesh = make_disk_mesh()
	from hedge.mesh import make_rect_mesh
	mesh = make_rect_mesh(a=(-1,-1),b=(1,1),max_area=0.008)

    if rcon.is_head_rank:
        mesh_data = rcon.distribute_mesh(mesh)
    else:
        mesh_data = rcon.receive_mesh()

    # discretization setup ----------------------------------------------------
    discr = rcon.make_discretization(mesh_data, order=4)
    vis_discr = discr
  
    # space-dependent-velocity-field ------------------------------------------
    # simple vortex 
    class VField:
        shape = (2,)
        
        def __call__(self, pt, el):
            x, y = pt
            # Correction-Factor to make the speed zero on the on the boundary
            #fac = (1-x**2)*(1-y**2) 
            fac = 1
            return (-y*fac, x*fac)

    v = discr.interpolate_volume_function(VField())

    # visualization setup -----------------------------------------------------
    from hedge.visualization import VtkVisualizer, SiloVisualizer
    vis = VtkVisualizer(vis_discr, rcon, "fld")

    # operator setup ----------------------------------------------------------
    from hedge.data import \
            ConstantGivenFunction, \
            TimeConstantGivenFunction, \
            TimeDependentGivenFunction
    from hedge.pde import SpaceDependentWeakAdvectionOperator
    op = SpaceDependentWeakAdvectionOperator(dim, v, flux_type="lf")

    # initial condition -------------------------------------------------------
    if False:
        def initial(pt, el):
            from math import exp
            x = (pt-numpy.array([0.3, 0.7]))*8
            return exp(-numpy.dot(x, x))
    if True:
        def initial(pt, el):
            x, y = pt
            if abs(x) < 0.5 and abs(y) < 0.2:
                return 2
            else:
                return 1

    u = discr.interpolate_volume_function(initial)
    
    # timestep setup ----------------------------------------------------------
    stepper = RK4TimeStepper()

    dt = discr.dt_factor(op.max_eigenvalue())
    nsteps = int(700/dt)

    if rcon.is_head_rank:
        print "%d elements, dt=%g, nsteps=%d" % (
                len(discr.mesh.elements),
                dt,
                nsteps)

    # filter setup-------------------------------------------------------------
    from hedge.discretization import Filter, ExponentialFilterResponseFunction
    antialiasing = Filter(discr,ExponentialFilterResponseFunction(min_amplification=0.9, order=4))

    # diagnostics setup -------------------------------------------------------
    from pytools.log import LogManager, \
            add_general_quantities, \
            add_simulation_quantities, \
            add_run_info

    logmgr = LogManager("advection.dat", "w", rcon.communicator)
    add_run_info(logmgr)
    add_general_quantities(logmgr)
    add_simulation_quantities(logmgr, dt)
    discr.add_instrumentation(logmgr)

    stepper.add_instrumentation(logmgr)

    from hedge.log import Integral, LpNorm
    u_getter = lambda: u
    logmgr.add_quantity(Integral(u_getter, discr, name="int_u"))
    logmgr.add_quantity(LpNorm(u_getter, discr, p=1, name="l1_u"))
    logmgr.add_quantity(LpNorm(u_getter, discr, name="l2_u"))

    logmgr.add_watches(["step.max", "t_sim.max", "l2_u", "t_step.max"])

    # timestep loop -----------------------------------------------------------
    rhs = op.bind(discr)
    for step in xrange(nsteps):
        logmgr.tick()

        t = step*dt

        if step % 10 == 0:
            visf = vis.make_file("fld-%04d" % step)
            vis.add_data(visf, [ ("u", u), ("v", v)], 
                        time=t, 
                        step=step
                        )
            visf.close()


        u = stepper(u, t, dt, rhs)
        # Use Filter:
        u = antialiasing(u)

    vis.close()

    logmgr.tick()
    logmgr.save()


if __name__ == "__main__":
    main()
