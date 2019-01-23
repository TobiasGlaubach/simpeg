from __future__ import print_function
from SimPEG import Problem, Mesh
from SimPEG import Utils
from SimPEG.Utils import mkvc, matutils, sdiag
from SimPEG import Props
import scipy as sp
import scipy.constants as constants
import os
import time
import numpy as np
import dask
import dask.array as da
from dask.diagnostics import ProgressBar
import multiprocessing

class GravityIntegral(Problem.LinearProblem):

    rho, rhoMap, rhoDeriv = Props.Invertible(
        "Specific density (g/cc)",
        default=1.
    )

    # surveyPair = Survey.LinearSurvey
    forwardOnly = False  # Is TRUE, forward matrix not stored to memory
    actInd = None  #: Active cell indices provided
    rxType = 'z'
    silent = False
    equiSourceLayer = False
    memory_saving_mode = False
    parallelized = "dask"
    n_cpu = None
    progressIndex = -1
    gtgdiag = None
    Jpath = "./sensitivity.zarr"
    maxRAM = 8  # Maximum memory usage
    verbose = True

    def __init__(self, mesh, **kwargs):
        Problem.BaseProblem.__init__(self, mesh, **kwargs)

    def fields(self, m):
        # self.model = self.rhoMap*m
        m = self.rhoMap*m
        if self.forwardOnly:

            # Compute the linear operation without forming the full dense G
            fields = self.Intrgl_Fwr_Op(m=m)

            return mkvc(fields)

        else:
            fields = da.dot(self.G, m)

            return np.array(fields, dtype='float')

    def modelMap(self):
        """
            Call for general mapping of the problem
        """
        return self.rhoMap

    def getJtJdiag(self, m, W=None):
        """
            Return the diagonal of JtJ
        """

        dmudm = self.rhoMap.deriv(m)
        self.model = m

        if self.gtgdiag is None:

            if W is None:
                w = np.ones(self.G.shape[1])
            else:
                w = W.diagonal()

            self.gtgdiag = da.sum(self.G**2., 0).compute()

            # for ii in range(self.G.shape[0]):

            #     self.gtgdiag += (w[ii]*self.G[ii, :]*dmudm)**2.

        return mkvc(np.sum((sdiag(mkvc(self.gtgdiag)**0.5) * dmudm).power(2.), axis=0))

    def getJ(self, m, f=None):
        """
            Sensitivity matrix
        """

        dmudm = self.rhoMap.deriv(m)

        return da.dot(self.G, dmudm)

    def Jvec(self, m, v, f=None):
        dmudm = self.rhoMap.deriv(m)

        vec = da.dot(self.G, (dmudm*v).astype(np.float32))

        return vec.astype(np.float64)

    def Jtvec(self, m, v, f=None):
        dmudm = self.rhoMap.deriv(m)

        vec = da.dot(self.G.T, v.astype(np.float32))
        return dmudm.T * vec.astype(np.float64)

    @property
    def G(self):
        if not self.ispaired:
            raise Exception('Need to pair!')

        if getattr(self, '_G', None) is None:

            self._G = self.Intrgl_Fwr_Op()

        return self._G

    def Intrgl_Fwr_Op(self, m=None, rxType='z'):

        """

        Gravity forward operator in integral form

        flag        = 'z' | 'xyz'

        Return
        _G        = Linear forward modeling operation

        Created on March, 15th 2016

        @author: dominiquef

         """

        if m is not None:
            self.model = self.rhoMap*m

        if getattr(self, 'actInd', None) is not None:

            if self.actInd.dtype == 'bool':
                inds = np.asarray([inds for inds,
                                  elem in enumerate(self.actInd, 1)
                                  if elem], dtype=int) - 1
            else:
                inds = self.actInd

        else:

            inds = np.asarray(range(self.mesh.nC))

        self.nC = len(inds)

        # Create active cell projector
        P = sp.sparse.csr_matrix(
            (np.ones(self.nC), (inds, range(self.nC))),
            shape=(self.mesh.nC, self.nC)
        )

        # Create vectors of nodal location
        # (lower and upper corners for each cell)
        if isinstance(self.mesh, Mesh.TreeMesh):
            # Get upper and lower corners of each cell
            bsw = (self.mesh.gridCC - self.mesh.h_gridded/2.)
            tne = (self.mesh.gridCC + self.mesh.h_gridded/2.)

            xn1, xn2 = bsw[:, 0], tne[:, 0]
            yn1, yn2 = bsw[:, 1], tne[:, 1]
            zn1, zn2 = bsw[:, 2], tne[:, 2]

        else:

            xn = self.mesh.vectorNx
            yn = self.mesh.vectorNy
            zn = self.mesh.vectorNz

            yn2, xn2, zn2 = np.meshgrid(yn[1:], xn[1:], zn[1:])
            yn1, xn1, zn1 = np.meshgrid(yn[:-1], xn[:-1], zn[:-1])

        # If equivalent source, use semi-infite prism
        if self.equiSourceLayer:
            zn1 -= 1000.

        self.Yn = P.T*np.c_[Utils.mkvc(yn1), Utils.mkvc(yn2)]
        self.Xn = P.T*np.c_[Utils.mkvc(xn1), Utils.mkvc(xn2)]
        self.Zn = P.T*np.c_[Utils.mkvc(zn1), Utils.mkvc(zn2)]

        self.rxLoc = self.survey.srcField.rxList[0].locs
        self.nD = int(self.rxLoc.shape[0])

        # if self.n_cpu is None:
        #     self.n_cpu = multiprocessing.cpu_count()

        # Switch to determine if the process has to be run in parallel
        job = Forward(
                rxLoc=self.rxLoc, Xn=self.Xn, Yn=self.Yn, Zn=self.Zn,
                n_cpu=self.n_cpu, forwardOnly=self.forwardOnly,
                model=self.model, rxType=self.rxType,
                parallelized=self.parallelized,
                verbose=self.verbose, Jpath=self.Jpath, maxRAM=self.maxRAM
                )

        G = job.calculate()

        return G

    # @property
    # def mapPair(self):
    #     """
    #         Call for general mapping of the problem
    #     """
    #     return self.rhoMap


class Forward(object):
    """
        Add docstring once it works
    """

    progressIndex = -1
    parallelized = "dask"
    rxLoc = None
    Xn, Yn, Zn = None, None, None
    n_cpu = None
    forwardOnly = False
    model = None
    rxType = 'z'
    verbose = True
    maxRAM = 8
    storeG = True
    Jpath = "./sensitivity.zarr"

    def __init__(self, **kwargs):
        super(Forward, self).__init__()
        Utils.setKwargs(self, **kwargs)

    def calculate(self):

        self.nD = self.rxLoc.shape[0]
        self.nC = self.Xn.shape[0]

        if self.n_cpu is None:
            self.n_cpu = int(multiprocessing.cpu_count())

        nChunks = self.n_cpu  # Number of chunks
        rowChunk, colChunk = int(self.nD/nChunks), int(self.nC/nChunks)  # Chunk sizes
        totRAM = rowChunk*colChunk*8*self.n_cpu*1e-9
        while totRAM > self.maxRAM:
            nChunks *= 2
            rowChunk, colChunk = int(np.ceil(self.nD/nChunks)), int(np.ceil(self.nC/nChunks)) # Chunk sizes
            totRAM = rowChunk*colChunk*8*self.n_cpu*1e-9
        print(self.n_cpu, rowChunk,  colChunk, totRAM,  self.maxRAM)
        if self.parallelized:

            # print(chunkSize)
            assert self.parallelized in ["dask", "multiprocessing"], (
                "'parallelization' must be 'dask', 'multiprocessing' or None"
                "Value provided -> "
                "{}".format(
                    self.parallelized)

            )

            if self.parallelized == "dask":

                if os.path.exists(self.Jpath):
                    print("Load G from zarr")
                    G = da.from_zarr(self.Jpath)

                else:

                    row = dask.delayed(self.calcTrow, pure=True)

                    makeRows = [row(self.rxLoc[ii, :]) for ii in range(self.nD)]
                    buildMat = [da.from_delayed(makeRow, dtype=float, shape=(1, self.nC)) for makeRow in makeRows]

                    stack = da.vstack(buildMat)

                    # TO-DO: Find a way to create in chunks instead
                    stack = stack.rechunk((rowChunk, colChunk))

                    if self.storeG:
                        with ProgressBar():
                            print("Saving G to zarr: "+ self.Jpath)
                            da.to_zarr(stack, self.Jpath)

                        G = da.from_zarr(self.Jpath)

                    else:
                        G = stack.compute()

            elif self.parallelized == "multiprocessing":

                pool = multiprocessing.Pool(self.n_cpu)

                result = pool.map(self.calcTrow, [self.rxLoc[ii, :] for ii in range(self.nD)])
                pool.close()
                pool.join()

                G = np.vstack(result)

        else:

            result = []
            for ii in range(self.nD):
                result += [self.calcTrow(self.rxLoc[ii, :])]
                self.progress(ii, self.nD)

            G = np.vstack(result)

        return G

    def calcTrow(self, xyzLoc):
        """
        Load in the active nodes of a tensor mesh and computes the gravity tensor
        for a given observation location xyzLoc[obsx, obsy, obsz]

        INPUT:
        Xn, Yn, Zn: Node location matrix for the lower and upper most corners of
                    all cells in the mesh shape[nC,2]
        M
        OUTPUT:
        Tx = [Txx Txy Txz]
        Ty = [Tyx Tyy Tyz]
        Tz = [Tzx Tzy Tzz]

        where each elements have dimension 1-by-nC.
        Only the upper half 5 elements have to be computed since symetric.
        Currently done as for-loops but will eventually be changed to vector
        indexing, once the topography has been figured out.

        """

        NewtG = constants.G*1e+8  # Convertion from mGal (1e-5) and g/cc (1e-3)
        eps = 1e-8  # add a small value to the locations to avoid

        # Pre-allocate space for 1D array
        row = np.zeros((1, self.Xn.shape[0]))

        dz = xyzLoc[2] - self.Zn

        dy = self.Yn - xyzLoc[1]

        dx = self.Xn - xyzLoc[0]

        # Compute contribution from each corners
        for aa in range(2):
            for bb in range(2):
                for cc in range(2):

                    r = (
                            mkvc(dx[:, aa]) ** 2 +
                            mkvc(dy[:, bb]) ** 2 +
                            mkvc(dz[:, cc]) ** 2
                        ) ** (0.50)

                    if self.rxType == 'x':
                        row -= NewtG * (-1) ** aa * (-1) ** bb * (-1) ** cc * (
                            dy[:, bb] * np.log(dz[:, cc] + r + eps) +
                            dz[:, cc] * np.log(dy[:, bb] + r + eps) -
                            dx[:, aa] * np.arctan(dy[:, bb] * dz[:, cc] /
                                                  (dx[:, aa] * r + eps)))

                    elif self.rxType == 'y':
                        row -= NewtG * (-1) ** aa * (-1) ** bb * (-1) ** cc * (
                            dx[:, aa] * np.log(dz[:, cc] + r + eps) +
                            dz[:, cc] * np.log(dx[:, aa] + r + eps) -
                            dy[:, bb] * np.arctan(dx[:, aa] * dz[:, cc] /
                                                  (dy[:, bb] * r + eps)))

                    else:
                        row -= NewtG * (-1) ** aa * (-1) ** bb * (-1) ** cc * (
                            dx[:, aa] * np.log(dy[:, bb] + r + eps) +
                            dy[:, bb] * np.log(dx[:, aa] + r + eps) -
                            dz[:, cc] * np.arctan(dx[:, aa] * dy[:, bb] /
                                                  (dz[:, cc] * r + eps)))

        if self.forwardOnly:
            return np.dot(row, self.model)
        else:
            return row

    def progress(self, ind, total):
        """
        progress(ind,prog,final)

        Function measuring the progress of a process and print to screen the %.
        Useful to estimate the remaining runtime of a large problem.

        Created on Dec, 20th 2015

        @author: dominiquef
        """
        arg = np.floor(ind/total*10.)
        if arg > self.progressIndex:
            print("Done " + str(arg*10) + " %")
            self.progressIndex = arg


class Problem3D_Diff(Problem.BaseProblem):
    """
        Gravity in differential equations!
    """

    _depreciate_main_map = 'rhoMap'

    rho, rhoMap, rhoDeriv = Props.Invertible(
        "Specific density (g/cc)",
        default=1.
    )

    solver = None

    def __init__(self, mesh, **kwargs):
        Problem.BaseProblem.__init__(self, mesh, **kwargs)

        self.mesh.setCellGradBC('dirichlet')

        self._Div = self.mesh.cellGrad

    @property
    def MfI(self): return self._MfI

    @property
    def Mfi(self): return self._Mfi

    def makeMassMatrices(self, m):
        self.model = m
        self._Mfi = self.mesh.getFaceInnerProduct()
        self._MfI = Utils.sdiag(1. / self._Mfi.diagonal())

    def getRHS(self, m):
        """


        """

        Mc = Utils.sdiag(self.mesh.vol)

        self.model = m
        rho = self.rho

        return Mc * rho

    def getA(self, m):
        """
        GetA creates and returns the A matrix for the Gravity nodal problem

        The A matrix has the form:

        .. math ::

            \mathbf{A} =  \Div(\MfMui)^{-1}\Div^{T}

        """
        return -self._Div.T * self.Mfi * self._Div

    def fields(self, m):
        """
            Return magnetic potential (u) and flux (B)
            u: defined on the cell nodes [nC x 1]
            gField: defined on the cell faces [nF x 1]

            After we compute u, then we update B.

            .. math ::

                \mathbf{B}_s = (\MfMui)^{-1}\mathbf{M}^f_{\mu_0^{-1}}\mathbf{B}_0-\mathbf{B}_0 -(\MfMui)^{-1}\Div^T \mathbf{u}

        """
        from scipy.constants import G as NewtG

        self.makeMassMatrices(m)
        A = self.getA(m)
        RHS = self.getRHS(m)

        if self.solver is None:
            m1 = sp.linalg.interface.aslinearoperator(
                Utils.sdiag(1 / A.diagonal())
            )
            u, info = sp.linalg.bicgstab(A, RHS, tol=1e-6, maxiter=1000, M=m1)

        else:
            print("Solving with Paradiso")
            Ainv = self.solver(A)
            u = Ainv * RHS

        gField = 4. * np.pi * NewtG * 1e+8 * self._Div * u

        return {'G': gField, 'u': u}
