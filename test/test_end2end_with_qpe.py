# -*- coding: utf-8 -*-

# Copyright 2018 IBM.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# =============================================================================

import unittest
from parameterized import parameterized
from collections import OrderedDict
import numpy as np
from qiskit_aqua.utils import decimal_to_binary
from qiskit_aqua import PluggableType, get_pluggable_class
from test.common import QiskitAquaChemistryTestCase
from qiskit_aqua_chemistry.drivers import ConfigurationManager
from qiskit_aqua_chemistry import FermionicOperator


class TestEnd2EndWithQPE(QiskitAquaChemistryTestCase):
    """QPE tests."""

    @parameterized.expand([
        [0.5],
        [0.735],
        [1],
    ])
    def test_qpe(self, distance):
        self.algorithm = 'QPE'
        self.log.debug(
            'Testing End-to-End with QPE on H2 with inter-atomic distance {}.'.format(distance))
        cfg_mgr = ConfigurationManager()
        pyscf_cfg = OrderedDict([
            ('atom', 'H .0 .0 .0; H .0 .0 {}'.format(distance)),
            ('unit', 'Angstrom'),
            ('charge', 0),
            ('spin', 0),
            ('basis', 'sto3g')
        ])
        section = {}
        section['properties'] = pyscf_cfg
        try:
            driver = cfg_mgr.get_driver_instance('PYSCF')
        except ModuleNotFoundError:
            self.skipTest('PYSCF driver does not appear to be installed')

        self.molecule = driver.run(section)

        ferOp = FermionicOperator(
            h1=self.molecule._one_body_integrals, h2=self.molecule._two_body_integrals)
        self.qubitOp = ferOp.mapping(
            map_type='PARITY', threshold=1e-10).two_qubit_reduced_operator(2)

        exact_eigensolver = get_pluggable_class(
            PluggableType.ALGORITHM, 'ExactEigensolver')(self.qubitOp, k=1)
        results = exact_eigensolver.run()
        self.reference_energy = results['energy']
        self.log.debug(
            'The exact ground state energy is: {}'.format(results['energy']))

        num_particles = self.molecule._num_alpha + self.molecule._num_beta
        two_qubit_reduction = True
        num_orbitals = self.qubitOp.num_qubits + \
            (2 if two_qubit_reduction else 0)
        qubit_mapping = 'parity'

        num_time_slices = 50
        n_ancillae = 9

        state_in = get_pluggable_class(PluggableType.INITIAL_STATE, 'HartreeFock')(
            self.qubitOp.num_qubits, num_orbitals, num_particles, qubit_mapping, two_qubit_reduction)
        iqft = get_pluggable_class(PluggableType.IQFT, 'STANDARD')(n_ancillae)

        qpe = get_pluggable_class(PluggableType.ALGORITHM, 'QPE')(
            self.qubitOp, state_in, iqft, num_time_slices, n_ancillae,
            paulis_grouping='random',
            expansion_mode='suzuki',
            expansion_order=2
        )
        qpe.setup_quantum_backend(backend='qasm_simulator', shots=100, skip_transpiler=True)

        result = qpe.run()

        self.log.debug('measurement results:      {}'.format(result['measurements']))
        self.log.debug('top result str label:     {}'.format(result['top_measurement_label']))
        self.log.debug('top result in decimal:    {}'.format(result['top_measurement_decimal']))
        self.log.debug('stretch:                  {}'.format(result['stretch']))
        self.log.debug('translation:              {}'.format(result['translation']))
        self.log.debug('final energy from QPE:    {}'.format(result['energy']))
        self.log.debug('reference energy:         {}'.format(self.reference_energy))
        self.log.debug('ref energy (transformed): {}'.format((self.reference_energy + result['translation']) * result['stretch']))
        self.log.debug('ref binary str label:     {}'.format(decimal_to_binary((self.reference_energy + result['translation']) * result['stretch'],
                       max_num_digits=n_ancillae + 3,
                       fractional_part_only=True)))

        np.testing.assert_approx_equal(
            result['energy'], self.reference_energy, significant=2)


if __name__ == '__main__':
    unittest.main()
