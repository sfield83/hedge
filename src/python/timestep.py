"""ODE solvers: timestepping support, such as Runge-Kutta, Adams-Bashforth, etc."""

from __future__ import division

__copyright__ = "Copyright (C) 2007 Andreas Kloeckner"

__license__ = """
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see U{http://www.gnu.org/licenses/}.
"""



import numpy
import numpy.linalg as la
from pytools import memoize




# coefficient generators ------------------------------------------------------
_RK4A = [0.0,
        -567301805773 /1357537059087,
        -2404267990393/2016746695238,
        -3550918686646/2091501179385,
        -1275806237668/ 842570457699,
        ]

_RK4B = [1432997174477/ 9575080441755,
        5161836677717 /13612068292357,
        1720146321549 / 2090206949498,
        3134564353537 / 4481467310338,
        2277821191437 /14882151754819,
        ]

_RK4C = [0.0,
        1432997174477/9575080441755,
        2526269341429/6820363962896,
        2006345519317/3224310063776,
        2802321613138/2924317926251,
        1,
        ]



def monomial_vdm(levels):
    class Monomial:
        def __init__(self, expt):
            self.expt = expt
        def __call__(self, x):
            return x**self.expt

    from hedge.polynomial import generic_vandermonde
    return generic_vandermonde(levels, 
            [Monomial(i) for i in range(len(levels))])




def make_interpolation_coefficients(levels, tap):
    point_eval_vec = numpy.array([ tap**n for n in range(len(levels))])
    return la.solve(monomial_vdm(levels).T, point_eval_vec)




def make_generic_ab_coefficients(levels, int_start, tap):
    point_eval_vec = numpy.array([
        1/(n+1)*(tap**(n+1)-int_start**(n+1))
        for n in range(len(levels))])

    return la.solve(monomial_vdm(levels).T, point_eval_vec)




@memoize
def make_ab_coefficients(order):
    return make_generic_ab_coefficients(numpy.arange(0, -order, -1), 0, 1)




# time steppers ---------------------------------------------------------------
class TimeStepper(object):
    pass




class RK4TimeStepper(TimeStepper):
    def __init__(self):
        from pytools.log import IntervalTimer
        self.timer = IntervalTimer(
                "t_rk4", "Time spent doing algebra in RK4")
        self.coeffs = zip(_RK4A, _RK4B, _RK4C)

    def add_instrumentation(self, logmgr):
        logmgr.add_quantity(self.timer)

    def __call__(self, y, t, dt, rhs):
        try:
            self.residual
        except AttributeError:
            self.residual = 0*rhs(t, y)

            from hedge.tools import is_mul_add_supported
            self.use_mul_add = is_mul_add_supported(self.residual)

        if self.use_mul_add:
            from hedge.tools import mul_add

            for a, b, c in self.coeffs:
                this_rhs = rhs(t + c*dt, y)

                self.timer.start()
                self.residual = mul_add(a, self.residual, dt, this_rhs)
                y = mul_add(1, y, b, self.residual)
                self.timer.stop()
        else:
            for a, b, c in self.coeffs:
                this_rhs = rhs(t + c*dt, y)

                self.timer.start()
                self.residual = a*self.residual + dt*this_rhs
                del this_rhs
                y = y + b * self.residual
                self.timer.stop()

        return y




class AdamsBashforthTimeStepper(TimeStepper):
    def __init__(self, order, startup_stepper=None):
        self.coefficients = make_ab_coefficients(order)
        self.f_history = []

        if startup_stepper is not None:
            self.startup_stepper = startup_stepper
        else:
            self.startup_stepper = RK4TimeStepper()

    def __call__(self, y, t, dt, rhs):
        if len(self.f_history) == 0:
            # insert IC
            self.f_history.append(rhs(t, y))

        if len(self.f_history) < len(self.coefficients):
            ynew = self.startup_stepper(y, t, dt, rhs)
            if len(self.f_history) == len(self.coefficients) - 1:
                # here's some memory we won't need any more
                del self.startup_stepper

        else:
            from operator import add

            assert len(self.coefficients) == len(self.f_history)
            ynew = y + dt * reduce(add,
                    (coeff * f 
                        for coeff, f in 
                        zip(self.coefficients, self.f_history)))

            self.f_history.pop()

        self.f_history.insert(0, rhs(t+dt, ynew))
        return ynew




class TwoRateAdamsBashforthTimeStepper(TimeStepper):
    """Simultaneously timesteps two parts of an ODE system,
    the first with a small timestep, the second with a large timestep.
    """

    def __init__(self, large_dt, step_ratio, order, startup_stepper=None):
        self.large_dt = large_dt
        self.small_dt = large_dt/step_ratio

        self.coefficients = make_ab_coefficients(order)
        self.substep_coefficients = [
                make_generic_ab_coefficients(
                    numpy.arange(0, -order, -1), 
                    (i-1)/step_ratio, 
                    i/step_ratio)
                for i in range(1, step_ratio+1)]

        # rhs_histories is row major--see documentation for 
        # rhss arg of __call__.
        self.rhs_histories = [[] for i in range(2*2)]

        if startup_stepper is not None:
            self.startup_stepper = startup_stepper
        else:
            self.startup_stepper = RK4TimeStepper()

        self.step_ratio = step_ratio

        self.startup_history = []

    def __call__(self, ys, t, rhss):
        """
        @arg rhss: Matrix of right-hand sides, stored in row-major order, i.e.
        C{[s2s, l2s, s2l, l2l]}.
        """
        from hedge.tools import make_obj_array

        if self.startup_stepper is not None:
            ys = make_obj_array(ys)

            def combined_rhs(t, y):
                return make_obj_array([rhs(t, *y) for rhs in rhss])

            def combined_summed_rhs(t, y):
                return numpy.sum(combined_rhs(t, y).reshape((2,2), order="C"), axis=1)

            for i in range(self.step_ratio):
                ys = self.startup_stepper(ys, t+i*self.small_dt, self.small_dt, 
                        combined_summed_rhs)
                self.startup_history.insert(0, combined_rhs(t+(i+1)*self.small_dt, ys))

            if len(self.startup_history) == len(self.coefficients)*self.step_ratio:
                # we're done starting up, pack data into split histories
                hist_s2s, hist_l2s, hist_s2l, hist_l2l = zip(*self.startup_history)

                n = len(self.coefficients)
                self.rhs_histories = [
                        list(hist_s2s[:n]),
                        list(hist_l2s[::self.step_ratio]),
                        list(hist_s2l[::self.step_ratio]),
                        list(hist_l2l[::self.step_ratio]),
                        ]

                from pytools import single_valued
                assert single_valued(len(h) for h in self.rhs_histories) == n

                # here's some memory we won't need any more
                self.startup_stepper = None
                del self.startup_history

            return ys
        else:
            rhs_s2s, rhs_l2s, rhs_s2l, rhs_l2l = rhss
            y_small, y_large = ys
            hist_s2s, hist_l2s, hist_s2l, hist_l2l = self.rhs_histories

            def rotate_insert(l, new_item):
                l.pop()
                l.insert(0, new_item)

            def linear_comb(coefficients, vectors):
                from operator import add
                return reduce(add,
                        (coeff * v for coeff, v in 
                            zip(coefficients, vectors)))

            coeff = self.coefficients

            # substep the 'small dt' part
            for i in range(self.step_ratio):
                sub_coeff = self.substep_coefficients[i]
                y_small = y_small + (
                        self.small_dt * linear_comb(coeff, hist_s2s)
                        + self.large_dt * linear_comb(sub_coeff, hist_l2s)
                        )

                if i == self.step_ratio-1:
                    break

                y_large_this_substep = None
                #y_large_this_substep = y_large + (
                        #self.large_dt * linear_comb(something, hist_l2l)
                        #+ self.large_dt * linear_comb(something, hist_s2l))

                rotate_insert(hist_s2s,
                        rhs_s2s(t+(i+1)*self.small_dt, y_small, y_large_this_substep))

            # step the 'large' part
            y_large = y_large + self.large_dt * (
                    linear_comb(coeff, hist_l2l) + linear_comb(coeff, hist_s2l))

            # calculate all right hand sides involving the 'large dt' part
            rotate_insert(hist_s2l, rhs_s2l(t+self.large_dt, y_small, y_large))
            rotate_insert(hist_l2l, rhs_l2l(t+self.large_dt, y_small, y_large))
            rotate_insert(hist_l2s, rhs_l2s(t+self.large_dt, y_small, y_large))

            # calculate the last 'small dt' rhs using the new 'large' data
            rotate_insert(hist_s2s, rhs_s2s(t+self.large_dt, y_small, y_large))

            return make_obj_array([y_small, y_large])
