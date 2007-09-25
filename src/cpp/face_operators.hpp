// Hedge - the Hybrid'n'Easy DG Environment
// Copyright (C) 2007 Andreas Kloeckner
// 
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
// 
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
// 
// You should have received a copy of the GNU General Public License
// along with this program.  If not, see <http://www.gnu.org/licenses/>.




#ifndef _ASFAHDALSU_HEDGE_FACE_OPERATORS_HPP_INCLUDED
#define _ASFAHDALSU_HEDGE_FACE_OPERATORS_HPP_INCLUDED




#include <boost/foreach.hpp>
#include <vector>
#include <utility>
#include "base.hpp"
#include "flux.hpp"




namespace hedge 
{
  typedef std::vector<unsigned> index_list;

  struct face_pair
  {
    face_pair()
      : opp_flux_face(0)
    { }

    index_list face_indices;
    index_list opposite_indices;
    fluxes::face flux_face;
    fluxes::face *opp_flux_face;
  };

  typedef std::vector<face_pair> face_group;
  /*
  struct face_group 
  {

    std::vector<face_pair> m_face_infos;

    unsigned size()
    {
      return m_face_infos.size();
    }

    void clear()
    {
      m_face_infos.clear();
    }

    void add_face(const index_list &my_ind, const index_list &opp_ind, 
        const fluxes::face &face)
    {
      face_pair fp;
      fp.face_indices = my_ind;
      fp.opposite_indices = opp_ind;
      fp.flux_face = face;
      fp.opp_flux_face = 0;
      m_face_infos.push_back(fp);
    }

    typedef std::pair<unsigned, unsigned> connection;
    typedef std::vector<connection> connection_list;

    void connect_faces(const connection_list &cnx_list)
    {
      BOOST_FOREACH(const connection &cnx, cnx_list)
        m_face_infos[cnx.first].opp_flux_face = &m_face_infos[cnx.second].flux_face;
    }
  };
  */




  template <class LFlux, class LTarget, class NFlux, class NTarget>
  struct flux_data
  {
    typedef LFlux local_flux_t;
    typedef NFlux neighbor_flux_t;
    typedef LTarget local_target_t;
    typedef NTarget neighbor_target_t;

    local_flux_t local_flux;
    local_target_t local_target;
    
    neighbor_flux_t neighbor_flux;
    neighbor_target_t neighbor_target;

    flux_data(LFlux lflux, LTarget ltarget, NFlux nflux, NTarget ntarget)
      : local_flux(lflux), local_target(ltarget), 
      neighbor_flux(nflux), neighbor_target(ntarget)
    { }
  };




  template <class LFlux, class LTarget, class NFlux, class NTarget>
  flux_data<LFlux, LTarget, NFlux, NTarget> make_flux_data(
      LFlux lflux, LTarget ltarget, NFlux nflux, NTarget ntarget)
  {
    return flux_data<LFlux, LTarget, NFlux, NTarget>(lflux, ltarget, nflux, ntarget);
  }




  template <class Mat, class FData>
  inline
  void perform_flux(const face_group &fg, const Mat &fmm, FData fdata)
  {
    unsigned face_length = fmm.size1();

    assert(fmm.size1() == fmm.size2());

    BOOST_FOREACH(const face_pair &fp, fg)
    {
      const double local_coeff = 
        fp.flux_face.face_jacobian*fdata.local_flux(fp.flux_face, fp.opp_flux_face);
      const double neighbor_coeff = 
        fp.flux_face.face_jacobian*fdata.neighbor_flux(fp.flux_face, fp.opp_flux_face);

      assert(fmm.size1() == fp.face_indices.size());
      assert(fmm.size1() == fp.opp_indices.size());

      for (unsigned i = 0; i < face_length; i++)
        for (unsigned j = 0; j < face_length; j++)
        {
          fdata.local_target.add_coefficient(fp.face_indices[i], fp.face_indices[j],
              local_coeff*fmm(i, j));
          fdata.neighbor_target.add_coefficient(fp.face_indices[i], fp.opposite_indices[j],
              neighbor_coeff*fmm(i, j));
        }
    }
  }




  template <class Mat, class LFlux, class LTarget, class NFlux, class NTarget>
  void perform_flux_detailed(const face_group &fg, const Mat& fmm,
      LFlux lflux, LTarget ltarget, NFlux nflux, NTarget ntarget)
  {
    perform_flux(fg, fmm, make_flux_data(lflux, ltarget, nflux, ntarget));
  }




}




#endif
