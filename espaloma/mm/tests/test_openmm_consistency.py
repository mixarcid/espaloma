import numpy as np
import numpy.testing as npt
import pytest
import torch
from simtk import openmm
from simtk import openmm as mm
from simtk import unit

from espaloma.utils.geometry import _sample_four_particle_torsion_scan

angle_unit = unit.radian
energy_unit = unit.kilojoule_per_mole

from simtk.openmm import app

import espaloma as esp


def _create_torsion_sim(
        periodicity: int = 2,
        phase=0 * angle_unit,
        k=10.0 * energy_unit
) -> app.Simulation:
    """Create a 4-particle OpenMM Simulation containing only a PeriodicTorsionForce"""
    system = mm.System()

    # add 4 particles of unit mass
    for _ in range(4):
        system.addParticle(1)

    # add torsion force to system
    force = mm.PeriodicTorsionForce()
    force.addTorsion(0, 1, 2, 3, periodicity, phase, k)
    system.addForce(force)

    # create openmm Simulation, which requires a Topology and Integrator
    topology = app.Topology()
    chain = topology.addChain()
    residue = topology.addResidue('torsion', chain)
    for name in ['a', 'b', 'c', 'd']:
        topology.addAtom(name, 'C', residue)
    integrator = mm.VerletIntegrator(1.0)
    sim = app.Simulation(topology, system, integrator)

    return sim


# TODO: mark this properly: want to test periodicities 1..6, +ve, -ve k
# @pytest.mark.parametrize(periodicity=[1,2,3,4,5,6], k=[-10 * energy_unit, +10 * energy_unit])
def test_periodic_torsion(periodicity=4, k=-10 * energy_unit, n_samples=100):
    phase = 0 * angle_unit
    sim = _create_torsion_sim(periodicity=periodicity, phase=phase, k=k)
    xyz_np = _sample_four_particle_torsion_scan(n_samples)

    # compute energies using OpenMM
    openmm_energies = np.zeros(n_samples)
    for i, pos in enumerate(xyz_np):
        sim.context.setPositions(pos)
        openmm_energies[i] = sim.context.getState(getEnergy=True).getPotentialEnergy() / energy_unit

    # compute energies using espaloma
    xyz = torch.tensor(xyz_np)
    x0, x1, x2, x3 = xyz[:, 0, :], xyz[:, 1, :], xyz[:, 2, :], xyz[:, 3, :]
    theta = esp.mm.geometry.dihedral(x0, x1, x2, x3).reshape((n_samples, 1))
    ks = torch.zeros(6)
    ks[periodicity - 1] = k / esp.units.ENERGY_UNIT

    # TODO: currently failing with run-time tensor index errors in esp.mm.functional.periodic
    espaloma_energies = esp.mm.functional.periodic(theta, ks) * esp.units.ENERGY_UNIT

    np.testing.assert_almost_equal(actual=espaloma_energies.numpy() / energy_unit, desired=openmm_energies)


@pytest.mark.parametrize(
    "g", esp.data.esol(first=10),
)
def test_energy_angle_and_bond(g):
    # make simulation
    from espaloma.data.md import MoleculeVacuumSimulation

    # get simulation
    simulation = MoleculeVacuumSimulation(
        n_samples=1, n_steps_per_sample=10
    ).simulation_from_graph(g)

    system = simulation.system

    forces = list(system.getForces())

    energies = {}

    for idx, force in enumerate(forces):
        force.setForceGroup(idx)

        name = force.__class__.__name__

        if "Nonbonded" in name:
            force.setNonbondedMethod(openmm.NonbondedForce.NoCutoff)

            # epsilons = {}
            # sigmas = {}

            # for _idx in range(force.getNumParticles()):
            #     q, sigma, epsilon = force.getParticleParameters(_idx)

            #     # record parameters
            #     epsilons[_idx] = epsilon
            #     sigmas[_idx] = sigma

            #     force.setParticleParameters(_idx, 0., sigma, epsilon)

            # def sigma_combining_rule(sig1, sig2):
            #     return (sig1 + sig2) / 2

            # def eps_combining_rule(eps1, eps2):
            #     return np.sqrt(np.abs(eps1 * eps2))

            # for _idx in range(force.getNumExceptions()):
            #     idx0, idx1, q, sigma, epsilon = force.getExceptionParameters(
            #         _idx)
            #     force.setExceptionParameters(
            #         _idx,
            #         idx0,
            #         idx1,
            #         0.0,
            #         sigma_combining_rule(sigmas[idx0], sigmas[idx1]),
            #         eps_combining_rule(epsilons[idx0], epsilons[idx1])
            #     )

            # force.updateParametersInContext(_simulation.context)

    # create new simulation
    _simulation = openmm.app.Simulation(
        simulation.topology, system, openmm.VerletIntegrator(0.0),
    )

    _simulation.context.setPositions(
        simulation.context.getState(getPositions=True).getPositions()
    )

    for idx, force in enumerate(forces):
        name = force.__class__.__name__

        state = _simulation.context.getState(
            getEnergy=True, getParameters=True, groups=2 ** idx,
        )

        energy = state.getPotentialEnergy().value_in_unit(esp.units.ENERGY_UNIT)

        energies[name] = energy

    # parametrize
    ff = esp.graphs.legacy_force_field.LegacyForceField("smirnoff99Frosst")
    g = ff.parametrize(g)

    # n2 : bond, n3: angle, n1: nonbonded?
    # n1 : sigma (k), epsilon (eq), and charge (not included yet)
    for term in ["n2", "n3"]:
        g.nodes[term].data["k"] = g.nodes[term].data["k_ref"]
        g.nodes[term].data["eq"] = g.nodes[term].data["eq_ref"]

    for term in ["n1"]:
        g.nodes[term].data["sigma"] = g.nodes[term].data["sigma_ref"]
        g.nodes[term].data["epsilon"] = g.nodes[term].data["epsilon_ref"]
        # g.nodes[term].data['q'] = g.nodes[term].data['q_ref']

    for term in ["n4"]:
        g.nodes[term].data["phases"] = g.nodes[term].data["phases_ref"]
        g.nodes[term].data["periodicity"] = g.nodes[term].data["periodicity_ref"]
        g.nodes[term].data["k"] = g.nodes[term].data["k_ref"]

    # for each atom, store n_snapshots x 3
    g.nodes["n1"].data["xyz"] = torch.tensor(
        simulation.context.getState(getPositions=True)
            .getPositions(asNumpy=True)
            .value_in_unit(esp.units.DISTANCE_UNIT),
        dtype=torch.float32,
    )[None, :, :].permute(1, 0, 2)

    # print(g.nodes['n2'].data)
    esp.mm.geometry.geometry_in_graph(g.heterograph)
    esp.mm.energy.energy_in_graph(g.heterograph)
    # writes into nodes
    # .data['u_nonbonded'], .data['u_onefour'], .data['u2'], .data['u3'],

    # test bonds
    npt.assert_almost_equal(
        g.nodes["g"].data["u_n2"].numpy(),
        energies["HarmonicBondForce"],
        decimal=3,
    )

    # test angles
    npt.assert_almost_equal(
        g.nodes["g"].data["u_n3"].numpy(),
        energies["HarmonicAngleForce"],
        decimal=3,
    )

    npt.assert_almost_equal(
        g.nodes["g"].data["u_n4"].numpy(),
        energies["PeriodicTorsionForce"],
        decimal=1,
    )

    print(g.nodes["g"].data["u_n4"].numpy())
    print(energies["PeriodicTorsionForce"])

    # TODO:
    # This is not working now, matching OpenMM nonbonded.
    # test nonbonded
    # TODO: must set all charges to zero in _simulation for this to pass currently, since g doesn't have any charges
    # npt.assert_almost_equal(
    #     g.nodes['g'].data['u_nonbonded'].numpy()\
    #     + g.nodes['g'].data['u_onefour'].numpy(),
    #     energies['NonbondedForce'],
    #     decimal=3,
    # )
