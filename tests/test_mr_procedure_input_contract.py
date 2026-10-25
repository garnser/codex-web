"""Nested protocol version compatibility; no application composition."""
import unittest
from copy import deepcopy
from pydantic import ValidationError
from codex_web.compatibility import ContractSpec, ContractCompatibilityError
from codex_web.skill_procedures import OperationalProcedure
from tests.test_skill_procedures import procedure


class MRProcedureInputContractTests(unittest.TestCase):
    def test_legacy_serialization_has_no_new_optional_field(self):
        for version in ('1.0','1.1'):
            payload=procedure();payload['schema_version']=version
            before=OperationalProcedure.model_validate(payload).model_dump(mode='json')
            self.assertNotIn('input_protocol',before)
            self.assertEqual(OperationalProcedure.model_validate(before).model_dump(mode='json'),before)
            self.assertEqual(before['schema_version'],version)

    def test_required_protocol_cannot_be_silently_downgraded(self):
        payload=procedure();payload.update(schema_version='1.2',input_protocol='code-host-mr-procedure-input.v1')
        current=OperationalProcedure.model_validate(payload)
        self.assertEqual(current.model_dump(mode='json')['input_protocol'],payload['input_protocol'])
        prior=ContractSpec('skill-operational-procedure','1.1',('1.0','1.1'))
        with self.assertRaises(ContractCompatibilityError):prior.require('1.2')
        for version in ('1.0','1.1'):
            with self.assertRaises(ValidationError):OperationalProcedure.model_validate({**payload,'schema_version':version})
        with self.assertRaises(ValidationError):OperationalProcedure.model_validate({**payload,'input_protocol':'self-stated-approval'})
        schema=OperationalProcedure.model_json_schema()
        # The existing canonical schema-driven editor renders, preserves and edits
        # this visible enum using the same mechanism as every other typed field.
        self.assertIn('input_protocol',schema['properties'])
        self.assertIn('code-host-mr-procedure-input.v1',str(schema['properties']['input_protocol']))
