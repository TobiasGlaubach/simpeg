"""
Parametric DC inversion with Dipole Dipole array
================================================

This is an example for a parametric inversion with a DC survey.
Resistivity structure of the subsurface is parameterized as following
parameters:

    - sigma_background: background conductivity
    - sigma_block: block conductivity
    - block_x0: horizotontal location of the block (center)
    - block_dx: width of the block
    - block_y0: depth of the block (center)
    - block_dy: thickness of the block

User is promoted to try different initial values of the parameterized model.
"""

from SimPEG import DC, Mesh
from SimPEG import (Maps, Utils, DataMisfit, Regularization,
                    Optimization, Inversion, InvProblem, Directives)
import matplotlib.pyplot as plt
from matplotlib import colors
import numpy as np
from pylab import hist
try:
    from pymatsolver import PardisoSolver as Solver
except ImportError:
    from SimPEG import SolverLU as Solver


def run(
    plotIt=True, survey_type="dipole-dipole",
    rho_background=1e3,
    rho_block=1e2,
    block_x0=100,
    block_dx=10,
    block_y0=-10,
    block_dy=5
):

    np.random.seed(1)
    # Initiate I/O class for DC
    IO = DC.IO()
    # Obtain ABMN locations

    xmin, xmax = 0., 200.
    ymin, ymax = 0., 0.
    zmin, zmax = 0, 0
    endl = np.array([[xmin, ymin, zmin], [xmax, ymax, zmax]])
    # Generate DC survey object
    survey = DC.Utils.gen_DCIPsurvey(endl, survey_type=survey_type, dim=2,
                                     a=10, b=10, n=10)
    survey.getABMN_locations()
    survey = IO.from_ambn_locations_to_survey(
        survey.a_locations, survey.b_locations,
        survey.m_locations, survey.n_locations,
        survey_type, data_dc_type='volt'
    )

    # Obtain 2D TensorMesh
    mesh, actind = IO.set_mesh()
    # Flat topography
    actind = Utils.surface2ind_topo(
        mesh, np.c_[mesh.vectorCCx, mesh.vectorCCx*0.]
    )
    survey.drapeTopo(mesh, actind, option="top")
    # Use Exponential Map: m = log(rho)
    actmap = Maps.InjectActiveCells(
        mesh, indActive=actind, valInactive=np.log(1e8)
    )
    parametric_block = Maps.ParametricBlock(mesh, slopeFact=1e2)
    mapping = Maps.ExpMap(mesh) * parametric_block
    # Set true model
    # val_background,val_block, block_x0, block_dx, block_y0, block_dy
    mtrue = np.r_[np.log(1e3), np.log(10), 100, 10, -20, 10]

    # Set initial model
    m0 = np.r_[
        np.log(rho_background), np.log(rho_block),
        block_x0, block_dx, block_y0, block_dy
    ]
    rho = mapping * mtrue
    rho0 = mapping * m0
    # Show the true conductivity model
    fig = plt.figure(figsize=(12, 3))
    ax = plt.subplot(111)
    temp = rho.copy()
    temp[~actind] = np.nan
    out = mesh.plotImage(
        temp, grid=False, ax=ax, gridOpts={'alpha': 0.2},
        clim=(10, 1000),
        pcolorOpts={"cmap": "viridis", "norm": colors.LogNorm()}
    )
    ax.plot(
        survey.electrode_locations[:, 0],
        survey.electrode_locations[:, 1], 'k.'
    )
    ax.set_xlim(IO.grids[:, 0].min(), IO.grids[:, 0].max())
    ax.set_ylim(-IO.grids[:, 1].max(), IO.grids[:, 1].min())
    cb = plt.colorbar(out[0])
    cb.set_label("Resistivity (ohm-m)")
    ax.set_aspect('equal')
    ax.set_title("True resistivity model")
    plt.show()
    # Show the true conductivity model
    fig = plt.figure(figsize=(12, 3))
    ax = plt.subplot(111)
    temp = rho0.copy()
    temp[~actind] = np.nan
    out = mesh.plotImage(
        temp, grid=False, ax=ax, gridOpts={'alpha': 0.2},
        clim=(10, 1000),
        pcolorOpts={"cmap": "viridis", "norm": colors.LogNorm()}
    )
    ax.plot(
        survey.electrode_locations[:, 0],
        survey.electrode_locations[:, 1], 'k.'
    )
    ax.set_xlim(IO.grids[:, 0].min(), IO.grids[:, 0].max())
    ax.set_ylim(-IO.grids[:, 1].max(), IO.grids[:, 1].min())
    cb = plt.colorbar(out[0])
    cb.set_label("Resistivity (ohm-m)")
    ax.set_aspect('equal')
    ax.set_title("Initial resistivity model")
    plt.show()

    # Generate 2.5D DC problem
    # "N" means potential is defined at nodes
    prb = DC.Problem2D_N(
        mesh, rhoMap=mapping, storeJ=True,
        Solver=Solver
    )
    # Pair problem with survey
    try:
        prb.pair(survey)
    except:
        survey.unpair()
        prb.pair(survey)

    # Make synthetic DC data with 5% Gaussian noise
    dtrue = survey.makeSyntheticData(mtrue, std=0.05, force=True)

    # Show apparent resisitivty pseudo-section
    IO.plotPseudoSection(
        data=survey.dobs/IO.G, data_type='apparent_resistivity'
    )

    # Show apparent resisitivty histogram
    fig = plt.figure()
    out = hist(survey.dobs/IO.G, bins=20)
    plt.show()
    # Set uncertainty
    # floor
    eps = 10**(-3.2)
    # percentage
    std = 0.05
    dmisfit = DataMisfit.l2_DataMisfit(survey)
    uncert = abs(survey.dobs) * std + eps
    dmisfit.W = 1./uncert

    # Map for a regularization
    mesh_1d = Mesh.TensorMesh([parametric_block.nP])
    # Related to inversion
    reg = Regularization.Simple(mesh_1d, alpha_x=0.)
    opt = Optimization.InexactGaussNewton(maxIter=10)
    invProb = InvProblem.BaseInvProblem(dmisfit, reg, opt)
    beta = Directives.BetaSchedule(coolingFactor=5, coolingRate=2)
    betaest = Directives.BetaEstimate_ByEig(beta0_ratio=1e0)
    target = Directives.TargetMisfit()
    updateSensW = Directives.UpdateSensitivityWeights()
    update_Jacobi = Directives.UpdatePreconditioner()
    invProb.beta = 0.
    inv = Inversion.BaseInversion(
        invProb, directiveList=[
            target
        ]
        )
    prb.counter = opt.counter = Utils.Counter()
    opt.LSshorten = 0.5
    opt.remember('xc')

    # Run inversion
    mopt = inv.run(m0)

    # Convert obtained inversion model to resistivity
    # rho = M(m), where M(.) is a mapping

    rho_est = mapping*mopt
    rho_true = rho.copy()
    # show recovered conductivity
    vmin, vmax = rho.min(), rho.max()
    fig, ax = plt.subplots(2, 1, figsize=(20, 6))
    out1 = mesh.plotImage(
            rho_true, clim=(10, 1000),
            pcolorOpts={"cmap": "viridis", "norm": colors.LogNorm()},
            ax=ax[0]
    )
    out2 = mesh.plotImage(
        rho_est, clim=(10, 1000),
        pcolorOpts={"cmap": "viridis", "norm": colors.LogNorm()},
        ax=ax[1]
    )
    out = [out1, out2]
    for i in range(2):
        ax[i].plot(
            survey.electrode_locations[:, 0],
            survey.electrode_locations[:, 1], 'kv'
        )
        ax[i].set_xlim(IO.grids[:, 0].min(), IO.grids[:, 0].max())
        ax[i].set_ylim(-IO.grids[:, 1].max(), IO.grids[:, 1].min())
        cb = plt.colorbar(out[i][0], ax=ax[i])
        cb.set_label("Resistivity ($\Omega$m)")
        ax[i].set_xlabel("Northing (m)")
        ax[i].set_ylabel("Elevation (m)")
        ax[i].set_aspect('equal')
    ax[0].set_title("True resistivity model")
    ax[1].set_title("Recovered resistivity model")
    plt.tight_layout()
    plt.show()


if __name__ == '__main__':
    run()
    plt.show()