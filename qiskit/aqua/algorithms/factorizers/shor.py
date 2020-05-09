# -*- coding: utf-8 -*-

# This code is part of Qiskit.
#
# (C) Copyright IBM 2019, 2020.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Shor's factoring algorithm."""

from typing import Optional, Union
import math
import array
import fractions
import logging
import numpy as np

from qiskit import ClassicalRegister, QuantumCircuit, QuantumRegister
from qiskit.circuit import Qubit
from qiskit.circuit.library import QFT
from qiskit.circuit.gate import Gate
from qiskit.providers import BaseBackend
from qiskit.aqua import QuantumInstance
from qiskit.aqua.utils.arithmetic import is_power
from qiskit.aqua.utils import get_subsystem_density_matrix
from qiskit.aqua.algorithms import QuantumAlgorithm, AlgorithmResult
from qiskit.aqua.utils import summarize_circuits
from qiskit.aqua.utils.validation import validate_min

logger = logging.getLogger(__name__)

# pylint: disable=invalid-name


class Shor(QuantumAlgorithm):
    """Shor's factoring algorithm.

    Shor's Factoring algorithm is one of the most well-known quantum algorithms and finds the
    prime factors for input integer :math:`N` in polynomial time.

    The input integer :math:`N` to be factored is expected to be odd and greater than 2.
    Even though this implementation is general, its capability will be limited by the
    capacity of the simulator/hardware. Another input integer :math:`a`  can also be supplied,
    which needs to be a co-prime smaller than :math:`N` .

    Adapted from https://github.com/ttlion/ShorAlgQiskit

    See also https://arxiv.org/abs/quant-ph/0205095
    """

    def __init__(self,
                 N: int = 15,
                 a: int = 2,
                 quantum_instance: Optional[Union[QuantumInstance, BaseBackend]] = None) -> None:
        """
        Args:
            N: The integer to be factored, has a min. value of 3.
            a: A random integer that satisfies a < N and gcd(a, N) = 1, has a min. value of 2.
            quantum_instance: Quantum Instance or Backend

         Raises:
            ValueError: Invalid input
        """
        validate_min('N', N, 3)
        validate_min('a', a, 2)
        super().__init__(quantum_instance)
        self._n = None
        self._up_qreg = None
        self._down_qreg = None
        self._aux_qreg = None

        # check the input integer
        if N < 1 or N % 2 == 0:
            raise ValueError('The input needs to be an odd integer greater than 1.')

        self._N = N

        if a >= N or math.gcd(a, self._N) != 1:
            raise ValueError('The integer a needs to satisfy a < N and gcd(a, N) = 1.')

        self._a = a

        self._ret = AlgorithmResult({"factors": [], "results": {}})

        # check if the input integer is a power
        tf, b, p = is_power(N, return_decomposition=True)
        if tf:
            logger.info('The input integer is a power: %s=%s^%s.', N, b, p)
            self._ret['factors'].append(b)

        self._qft = QFT(do_swaps=False)
        self._iqft = self._qft.inverse()

        self._phi_add = None
        self._iphi_add = None

    def _init_circuit(self) -> QuantumCircuit:
        return QuantumCircuit(self._up_qreg, self._down_qreg, self._aux_qreg)

    def _get_angles(self, a: int) -> np.ndarray:
        """Calculate the array of angles to be used in the addition in Fourier Space."""
        s = bin(int(a))[2:].zfill(self._n + 1)
        angles = np.zeros([self._n + 1])
        for i in range(0, self._n + 1):
            for j in range(i, self._n + 1):
                if s[j] == '1':
                    angles[self._n - i] += math.pow(2, -(j - i))
            angles[self._n - i] *= np.pi
        return angles

    def _phi_add_gate(self, q: QuantumRegister, a: int) -> Gate:
        """Creation of the gate that performs addition by a in Fourier Space."""
        circuit = QuantumCircuit(q, name="phi_add")
        angle = self._get_angles(a)
        for i in range(self._n + 1):
            circuit.u1(angle[i], q[i])
        return circuit.to_gate()

    def _controlled_controlled_phi_add_mod_N(self,
                                             circuit: QuantumCircuit,
                                             aux: QuantumRegister,
                                             ctl_down: Qubit,
                                             ctl_up: Qubit,
                                             ctl_aux: Qubit,
                                             a: int):
        """Circuit that implements doubly controlled modular addition by a."""
        qubits = [aux[i] for i in reversed(range(self._n + 1))]
        circuit.compose(self._phi_add_gate(aux, a).control(2), [*aux._bits, ctl_down, ctl_up], inplace=True)

        circuit.compose(self._iphi_add, aux, inplace=True)
        circuit.compose(self._iqft, qubits, inplace=True)

        circuit.cx(aux[self._n], ctl_aux)  # midpoint

        circuit.compose(self._qft, qubits, inplace=True)
        circuit.compose(self._phi_add_gate(aux, a).control(1), [*aux._bits, ctl_up], inplace=True)

        circuit.compose(self._phi_add_gate(aux, a).inverse().control(2), [*aux._bits, ctl_down, ctl_up], inplace=True)

        circuit.compose(self._iqft, qubits, inplace=True)

        circuit.x(aux[self._n])
        circuit.cx(aux[self._n], ctl_aux)  # midpoint 2
        circuit.x(aux[self._n])

        circuit.compose(self._qft, qubits, inplace=True)

        circuit.compose(self._phi_add_gate(aux, a).control(2), [*aux._bits, ctl_down, ctl_up], inplace=True)

        return circuit

    def _controlled_multiple_mod_N(self,
                                   ctl_up: Qubit,
                                   down: QuantumRegister,
                                   aux: QuantumRegister,
                                   a: int) -> QuantumCircuit:
        """Circuit that implements single controlled modular multiplication by a."""
        qubits = [aux[i] for i in reversed(range(self._n + 1))]

        add_mod_circuit = self._init_circuit()
        add_mod_circuit.compose(self._qft, qubits, inplace=True)

        for i in range(0, self._n):
            add_mod_circuit.extend(
            self._controlled_controlled_phi_add_mod_N(
                add_mod_circuit,
                aux,
                down[i],
                ctl_up,
                aux[self._n + 1],
                (2 ** i) * a % self._N
            ))

        add_mod_circuit.compose(self._iqft, qubits, inplace=True)

        for i in range(0, self._n):
            add_mod_circuit.cswap(ctl_up, down[i], aux[i])

        add_mod_circuit.extend(add_mod_circuit.inverse())
        return add_mod_circuit

    def construct_circuit(self, measurement: bool = False) -> QuantumCircuit:
        """Construct circuit.

        Args:
            measurement: Boolean flag to indicate if measurement should be included in the circuit.

        Returns:
            Quantum circuit.
        """

        # Get n value used in Shor's algorithm, to know how many qubits are used
        self._n = math.ceil(math.log(self._N, 2))
        self._qft.num_qubits = self._n + 1
        self._iqft.num_qubits = self._n + 1

        # quantum register where the sequential QFT is performed
        self._up_qreg = QuantumRegister(2 * self._n, name='up')
        # quantum register where the multiplications are made
        self._down_qreg = QuantumRegister(self._n, name='down')
        # auxiliary quantum register used in addition and multiplication
        self._aux_qreg = QuantumRegister(self._n + 2, name='aux')

        # Create Quantum Circuit
        circuit = self._init_circuit()

        self._phi_add = self._phi_add_gate(self._aux_qreg, self._N)
        self._iphi_add = self._phi_add.inverse()

        # Create maximal superposition in top register
        circuit.h(self._up_qreg)

        # Initialize down register to 1
        circuit.x(self._down_qreg[0])

        # Apply the multiplication gates as showed in
        # the report in order to create the exponentiation
        for i, ctl_up in enumerate(self._up_qreg):
            circuit.extend(self._controlled_multiple_mod_N(
                ctl_up,
                self._down_qreg,
                self._aux_qreg,
                int(pow(self._a, pow(2, i)))
            ))

        # Apply inverse QFT
        iqft = QFT(len(self._up_qreg)).inverse()
        circuit.compose(iqft, qubits=self._up_qreg)

        if measurement:
            up_cqreg = ClassicalRegister(2 * self._n, name='m')
            circuit.add_register(up_cqreg)
            circuit.measure(self._up_qreg, up_cqreg)

        logger.info(summarize_circuits(circuit))

        return circuit

    def _get_factors(self, output_desired: str, t_upper: int) -> bool:
        """Apply the continued fractions to find r and the gcd to find the desired factors."""
        x_value = int(output_desired, 2)
        logger.info('In decimal, x_final value for this result is: %s.', x_value)

        if x_value <= 0:
            self._ret['results'][output_desired] = \
                'x_value is <= 0, there are no continued fractions.'
            return False

        logger.debug('Running continued fractions for this case.')

        # Calculate T and x/T
        T = pow(2, t_upper)
        x_over_T = x_value / T

        # Cycle in which each iteration corresponds to putting one more term in the
        # calculation of the Continued Fraction (CF) of x/T

        # Initialize the first values according to CF rule
        i = 0
        b = array.array('i')
        t = array.array('f')

        b.append(math.floor(x_over_T))
        t.append(x_over_T - b[i])

        while i >= 0:

            # From the 2nd iteration onwards, calculate the new terms of the CF based
            # on the previous terms as the rule suggests
            if i > 0:
                b.append(math.floor(1 / t[i - 1]))
                t.append((1 / t[i - 1]) - b[i])

            # Calculate the CF using the known terms
            aux = 0
            j = i
            while j > 0:
                aux = 1 / (b[j] + aux)
                j = j - 1

            aux = aux + b[0]

            # Get the denominator from the value obtained
            frac = fractions.Fraction(aux).limit_denominator()
            denominator = frac.denominator

            logger.debug('Approximation number %s of continued fractions:', i + 1)
            logger.debug("Numerator:%s \t\t Denominator: %s.", frac.numerator, frac.denominator)

            # Increment i for next iteration
            i = i + 1

            if denominator % 2 == 1:
                if i >= self._N:
                    self._ret['results'][output_desired] = \
                        'unable to find factors after too many attempts.'
                    return False
                logger.debug('Odd denominator, will try next iteration of continued fractions.')
                continue

            # If denominator even, try to get factors of N
            # Get the exponential a^(r/2)
            exponential = 0

            if denominator < 1000:
                exponential = pow(self._a, denominator / 2)

            # Check if the value is too big or not
            if math.isinf(exponential) or exponential > 1000000000:
                self._ret['results'][output_desired] = \
                    'denominator of continued fraction is too big.'
                return False

            # If the value is not to big (infinity),
            # then get the right values and do the proper gcd()
            putting_plus = int(exponential + 1)
            putting_minus = int(exponential - 1)
            one_factor = math.gcd(putting_plus, self._N)
            other_factor = math.gcd(putting_minus, self._N)

            # Check if the factors found are trivial factors or are the desired factors
            if one_factor == 1 or one_factor == self._N or \
                    other_factor == 1 or other_factor == self._N:
                logger.debug('Found just trivial factors, not good enough.')
                # Check if the number has already been found,
                # use i-1 because i was already incremented
                if t[i - 1] == 0:
                    self._ret['results'][output_desired] = \
                        'the continued fractions found exactly x_final/(2^(2n)).'
                    return False
                if i >= self._N:
                    self._ret['results'][output_desired] = \
                        'unable to find factors after too many attempts.'
                    return False
            else:
                logger.debug('The factors of %s are %s and %s.', self._N, one_factor, other_factor)
                logger.debug('Found the desired factors.')
                self._ret['results'][output_desired] = (one_factor, other_factor)
                factors = sorted((one_factor, other_factor))
                if factors not in self._ret['factors']:
                    self._ret['factors'].append(factors)
                return True

    def _run(self) -> AlgorithmResult:
        if not self._ret['factors']:
            logger.debug('Running with N=%s and a=%s.', self._N, self._a)

            if self._quantum_instance.is_statevector:
                circuit = self.construct_circuit(measurement=False)
                logger.warning('The statevector_simulator might lead to '
                               'subsequent computation using too much memory.')
                result = self._quantum_instance.execute(circuit)
                complete_state_vec = result.get_statevector(circuit)
                # TODO: this uses too much memory
                up_qreg_density_mat = get_subsystem_density_matrix(
                    complete_state_vec,
                    range(2 * self._n, 4 * self._n + 2)
                )
                up_qreg_density_mat_diag = np.diag(up_qreg_density_mat)

                counts = dict()
                for i, v in enumerate(up_qreg_density_mat_diag):
                    if not v == 0:
                        counts[bin(int(i))[2:].zfill(2 * self._n)] = v ** 2
            else:
                circuit = self.construct_circuit(measurement=True)
                counts = self._quantum_instance.execute(circuit).get_counts(circuit)

            # For each simulation result, print proper info to user
            # and try to calculate the factors of N
            for output_desired in list(counts.keys()):
                # Get the x_value from the final state qubits
                logger.info("------> Analyzing result %s.", output_desired)
                self._ret['results'][output_desired] = None
                success = self._get_factors(output_desired, int(2 * self._n))
                if success:
                    logger.info('Found factors %s from measurement %s.',
                                self._ret['results'][output_desired], output_desired)
                else:
                    logger.info('Cannot find factors from measurement %s because %s',
                                output_desired, self._ret['results'][output_desired])

        return self._ret

from qiskit import Aer
shor = Shor(15, 11)
circ = shor.construct_circuit()
# result_dict = shor.run(QuantumInstance(Aer.get_backend("qasm_simulator"), shots=1000))