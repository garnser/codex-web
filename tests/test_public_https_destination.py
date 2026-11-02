"""Pure public-destination tests: no DNS, network, application or credentials."""
import socket
import unittest
from unittest.mock import Mock

from codex_web.public_https_destination import (
    PublicHTTPSDestinationDenied,
    resolve_public_https_destination,
    validate_public_https_host,
)


def answer(ip, *, port=443, family=None):
    family = family or (socket.AF_INET6 if ':' in ip else socket.AF_INET)
    address = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', address)


class PublicHTTPSDestinationTests(unittest.TestCase):
    def test_hostnames_are_exact_canonical_dns_names(self):
        self.assertEqual(validate_public_https_host('Files.PythonHosted.org'), 'files.pythonhosted.org')
        self.assertEqual(validate_public_https_host('xn--bcher-kva.example'), 'xn--bcher-kva.example')
        for value in ('', 'localhost', '127.0.0.1', '127.1', '2130706433', '0x7f.1',
                      '0x8.0x8.0x8.0x8', '[::1]', '::1', 'https://pypi.org', 'pypi.org:443', 'user@pypi.org',
                      '*.pypi.org', 'pypi.org.', '.pypi.org', 'pypi..org', ' pypi.org',
                      'pypi.org\n', 'py_pi.org', '-pypi.org', 'pypi-.org', 'bücher.example',
                      'x'*64 + '.org', None, 443):
            with self.subTest(host=value), self.assertRaises(PublicHTTPSDestinationDenied):
                validate_public_https_host(value)

    def test_all_public_results_become_immutable_numeric_routes(self):
        resolver = Mock(return_value=[answer('8.8.8.8'), answer('2606:4700:4700::1111'), answer('8.8.8.8')])
        routes = resolve_public_https_destination('PyPI.org', resolver=resolver)
        resolver.assert_called_once_with('pypi.org', 443, type=socket.SOCK_STREAM)
        self.assertEqual(routes, ((socket.AF_INET, socket.IPPROTO_TCP, ('8.8.8.8', 443)),
            (socket.AF_INET6, socket.IPPROTO_TCP, ('2606:4700:4700::1111', 443, 0, 0))))
        self.assertNotIn('pypi.org', repr(routes))
        # A later resolver result cannot alter the already returned route.
        resolver.return_value = [answer('127.0.0.1')]
        self.assertEqual(routes[0][2], ('8.8.8.8', 443))
        self.assertEqual(resolver.call_count, 1)

    def test_every_nonpublic_class_and_mapped_address_refuses(self):
        for ip in ('127.0.0.1', '10.0.0.1', '172.16.0.1', '192.168.1.1', '169.254.169.254',
                   '0.0.0.0', '224.0.0.1', '240.0.0.1', '100.64.0.1', '192.0.2.1',
                   '::', '::1', 'fc00::1', 'fe80::1', 'ff02::1', '2001:db8::1',
                   '::ffff:127.0.0.1', '::ffff:10.0.0.1', '::ffff:8.8.8.8',
                   '64:ff9b::7f00:1', '2002:7f00:1::', '2002:808:808::',
                   '2001:0:4136:e378:8000:63bf:3fff:fdd2'):
            with self.subTest(ip=ip), self.assertRaises(PublicHTTPSDestinationDenied):
                resolve_public_https_destination('pypi.org', resolver=Mock(return_value=[answer(ip)]))

    def test_mixed_results_refuse_whole_observation_in_both_orders(self):
        public, private = answer('8.8.8.8'), answer('127.0.0.1')
        for rows in ([public, private], [private, public], [public, answer('::1')]):
            with self.subTest(rows=rows), self.assertRaises(PublicHTTPSDestinationDenied):
                resolve_public_https_destination('pypi.org', resolver=Mock(return_value=rows))

    def test_ports_refuse_before_dns(self):
        resolver = Mock()
        for port in (80, 8443, 0, -1, 443.0, '443', True, None):
            with self.subTest(port=port), self.assertRaises(PublicHTTPSDestinationDenied):
                resolve_public_https_destination('pypi.org', port, resolver=resolver)
        resolver.assert_not_called()

    def test_malformed_or_incomplete_answers_never_return_partial_routes(self):
        valid = answer('8.8.8.8')
        malformed = [[], [valid]*65, [None], [()], [(socket.AF_UNIX, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))],
            [(socket.AF_INET, socket.SOCK_DGRAM, 17, '', ('8.8.8.8', 443))],
            [answer('pypi.org')], [answer('8.8.8.8', port=80)],
            [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('::1', 443))],
            [(socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('2606:4700::1111', 443, 0, 1))],
            [answer('2606:4700::1111%eth0')], [valid, ('bad',)]]
        for rows in malformed:
            with self.subTest(rows=rows), self.assertRaises(PublicHTTPSDestinationDenied):
                resolve_public_https_destination('pypi.org', resolver=Mock(return_value=rows))

    def test_resolution_failure_is_closed_and_does_not_leak_details(self):
        with self.assertRaises(PublicHTTPSDestinationDenied) as caught:
            resolve_public_https_destination('pypi.org', resolver=Mock(side_effect=socket.gaierror('private detail')))
        self.assertNotIn('private detail', str(caught.exception))
