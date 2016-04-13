#!/usr/bin/python
#
# This is code which tries very hard to interpret the From:, To: and Cc:
# lines found in real-world e-mail addresses and make sense of them.
#
# The general strategy of this script is to:
#    1. parse header into tokens
#    2. group tokens together into address + name constructs
#    3. normalize each group to a standard format
#
# In practice, we do this in two passes - first a strict pass where we try
# to parse things semi-sensibly.  If that fails, there is a second pass
# where we try to cope with certain types of weirdness we've seen in the
# wild. The wild can be pretty wild.
#
# This parser is NOT fully RFC2822 compliant - in particular it will get
# confused by nested comments (see FIXME in tests below).
#
import sys
import traceback

from mailpile.mailutils import AddressHeaderParser as AHP


ahp_tests = AHP(AHP.TEST_HEADER_DATA)
print '_tokens: {0!s}'.format(ahp_tests._tokens)
print '_groups: {0!s}'.format(ahp_tests._groups)
print '{0!s}'.format(ahp_tests)
print 'normalized: {0!s}'.format(ahp_tests.normalized())


headers, header, inheader = {}, None, False
for line in sys.stdin:
    if inheader:
        if line in ('\n', '\r\n'):
            for hdr in ('from', 'to', 'cc'):
                val = headers.get(hdr, '').replace('\n', ' ').strip()
                if val:
                    try:
                        nv = AHP(val, _raise=True).normalized()
                        if '\\' in nv:
                            print 'ESCAPED: {0!s}: {1!s} (was {2!s})'.format(hdr, nv, val)
                        else:
                            print '{0!s}'.format(nv)
                    except ValueError:
                        print 'FAILED: {0!s}: {1!s} -- {2!s}'.format(hdr, val,
                            traceback.format_exc().replace('\n', '  '))
            headers, header, inheader = {}, None, False
        elif line[:1] in (' ', '\t') and header:
            headers[header] = headers[header].rstrip() + line[1:]
        else:
            try:
                header, value = line.split(': ', 1)
                header = header.lower()
                headers[header] = headers.get(header, '') + ' ' + value
            except ValueError:
                headers, header, inheader = {}, None, False
    else:
        if line.startswith('From '):
            inheader = True
