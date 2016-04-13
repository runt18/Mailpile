# Copyright (C) 2001-2010 Python Software Foundation
# Contact: email-sig@python.org
#
# Updated/forked January 2014 by Bjarni R. Einarsson <bre@mailpile.is>
# to match the python 3.x email.generator CRLF control API (linesep=...).
#
# PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2
# --------------------------------------------
#
# 1. This LICENSE AGREEMENT is between the Python Software Foundation
# ("PSF"), and the Individual or Organization ("Licensee") accessing and
# otherwise using this software ("Python") in source or binary form and
# its associated documentation.
#
# 2. Subject to the terms and conditions of this License Agreement, PSF
# hereby grants Licensee a nonexclusive, royalty-free, world-wide
# license to reproduce, analyze, test, perform and/or display publicly,
# prepare derivative works, distribute, and otherwise use Python
# alone or in any derivative version, provided, however, that PSF's
# License Agreement and PSF's notice of copyright, i.e., "Copyright (c)
# 2001, 2002, 2003, 2004, 2005, 2006 Python Software Foundation; All Rights
# Reserved" are retained in Python alone or in any derivative version
# prepared by Licensee.
#
# 3. In the event Licensee prepares a derivative work that is based on
# or incorporates Python or any part thereof, and wants to make
# the derivative work available to others as provided herein, then
# Licensee hereby agrees to include in any such work a brief summary of
# the changes made to Python.
#
# 4. PSF is making Python available to Licensee on an "AS IS"
# basis.  PSF MAKES NO REPRESENTATIONS OR WARRANTIES, EXPRESS OR
# IMPLIED.  BY WAY OF EXAMPLE, BUT NOT LIMITATION, PSF MAKES NO AND
# DISCLAIMS ANY REPRESENTATION OR WARRANTY OF MERCHANTABILITY OR FITNESS
# FOR ANY PARTICULAR PURPOSE OR THAT THE USE OF PYTHON WILL NOT
# INFRINGE ANY THIRD PARTY RIGHTS.
#
# 5. PSF SHALL NOT BE LIABLE TO LICENSEE OR ANY OTHER USERS OF PYTHON
# FOR ANY INCIDENTAL, SPECIAL, OR CONSEQUENTIAL DAMAGES OR LOSS AS
# A RESULT OF MODIFYING, DISTRIBUTING, OR OTHERWISE USING PYTHON,
# OR ANY DERIVATIVE THEREOF, EVEN IF ADVISED OF THE POSSIBILITY THEREOF.
#
# 6. This License Agreement will automatically terminate upon a material
# breach of its terms and conditions.
#
# 7. Nothing in this License Agreement shall be deemed to create any
# relationship of agency, partnership, or joint venture between PSF and
# Licensee.  This License Agreement does not grant permission to use PSF
# trademarks or trade name in a trademark sense to endorse or promote
# products or services of Licensee, or any third party.
#
# 8. By copying, installing or otherwise using Python, Licensee
# agrees to be bound by the terms and conditions of this License
# Agreement.


"""Classes to generate plain text from a message object tree."""

__all__ = ['Generator', 'DecodedGenerator']

import re
import sys
import time
import random
import warnings

from cStringIO import StringIO
from email.header import Header

from mailpile.i18n import gettext as _
from mailpile.i18n import ngettext as _n


UNDERSCORE = '_'
NL = '\n'

fcre = re.compile(r'^From ', re.MULTILINE)


def _is8bitstring(s):
    if isinstance(s, str):
        try:
            unicode(s, 'us-ascii')
        except UnicodeError:
            return True
    return False


class Generator:
    """Generates output from a Message object tree.

    This basic generator writes the message to the given file object as plain
    text.
    """
    #
    # Public interface
    #

    def __init__(self, outfp,
                 mangle_from_=True, maxheaderlen=78, linesep=None):
        """Create the generator for message flattening.

        outfp is the output file-like object for writing the message to.  It
        must have a write() method.

        Optional mangle_from_ is a flag that, when True (the default), escapes
        From_ lines in the body of the message by putting a `>' in front of
        them.

        Optional maxheaderlen specifies the longest length for a non-continued
        header.  When a header line is longer (in characters, with tabs
        expanded to 8 spaces) than maxheaderlen, the header will split as
        defined in the Header class.  Set maxheaderlen to zero to disable
        header wrapping.  The default is 78, as recommended (but not required)
        by RFC 2822.
        """
        self._fp = outfp
        self._mangle_from_ = mangle_from_
        self._maxheaderlen = maxheaderlen
        self._NL = linesep or NL

    def write(self, s):
        # Just delegate to the file object
        self._fp.write(s)

    def flatten(self, msg, unixfrom=False, linesep=None):
        """Print the message object tree rooted at msg to the output file
        specified when the Generator instance was created.

        unixfrom is a flag that forces the printing of a Unix From_ delimiter
        before the first object in the message tree.  If the original message
        has no From_ delimiter, a `standard' one is crafted.  By default, this
        is False to inhibit the printing of any From_ delimiter.

        linesep specifies the characters used to indicate a new line in the
        output. The default value is LF (not the standard CRLF).

        Note that for subobjects, no From_ line is printed.
        """
        if linesep:
            self._NL = linesep
        if unixfrom:
            ufrom = msg.get_unixfrom()
            if not ufrom:
                ufrom = 'From nobody ' + time.ctime(time.time())
            print >> self._fp, ufrom + self._NL,
        self._write(msg)

    def clone(self, fp):
        """Clone this generator with the exact same options."""
        return self.__class__(fp, self._mangle_from_, self._maxheaderlen)

    #
    # Protected interface - undocumented ;/
    #

    def _write(self, msg):
        # We can't write the headers yet because of the following scenario:
        # say a multipart message includes the boundary string somewhere in
        # its body.  We'd have to calculate the new boundary /before/ we write
        # the headers so that we can write the correct Content-Type:
        # parameter.
        #
        # The way we do this, so as to make the _handle_*() methods simpler,
        # is to cache any subpart writes into a StringIO.  The we write the
        # headers and the StringIO contents.  That way, subpart handlers can
        # Do The Right Thing, and can still modify the Content-Type: header if
        # necessary.
        oldfp = self._fp
        try:
            self._fp = sfp = StringIO()
            self._dispatch(msg)
        finally:
            self._fp = oldfp
        # Write the headers.  First we see if the message object wants to
        # handle that itself.  If not, we'll do it generically.
        meth = getattr(msg, '_write_headers', None)
        if meth is None:
            self._write_headers(msg)
        else:
            meth(self)
        self._fp.write(sfp.getvalue())

    def _dispatch(self, msg):
        # Get the Content-Type: for the message, then try to dispatch to
        # self._handle_<maintype>_<subtype>().  If there's no handler for the
        # full MIME type, then dispatch to self._handle_<maintype>().  If
        # that's missing too, then dispatch to self._writeBody().
        main = msg.get_content_maintype()
        sub = msg.get_content_subtype()
        specific = UNDERSCORE.join((main, sub)).replace('-', '_')
        meth = getattr(self, '_handle_' + specific, None)
        if meth is None:
            generic = main.replace('-', '_')
            meth = getattr(self, '_handle_' + generic, None)
            if meth is None:
                meth = self._writeBody
        meth(msg)

    #
    # Default handlers
    #

    def _write_headers(self, msg):
        for h, v in msg.items():
            print >> self._fp, '{0!s}:'.format(h),
            if self._maxheaderlen == 0:
                # Explicit no-wrapping
                print >> self._fp, v + self._NL,
            elif isinstance(v, Header):
                # Header instances know what to do
                hdr = v.encode().replace('\n', self._NL)
                print >> self._fp, hdr + self._NL,
            elif _is8bitstring(v):
                # If we have raw 8bit data in a byte string, we have no idea
                # what the encoding is.  There is no safe way to split this
                # string.  If it's ascii-subset, then we could do a normal
                # ascii split, but if it's multibyte then we could break the
                # string.  There's no way to know so the least harm seems to
                # be to not split the string and risk it being too long.
                print >> self._fp, v + self._NL,
            else:
                # Header's got lots of smarts, so use it.  Note that this is
                # fundamentally broken though because we lose idempotency when
                # the header string is continued with tabs.  It will now be
                # continued with spaces.  This was reversedly broken before we
                # fixed bug 1974.  Either way, we lose.
                hdr = Header(v, maxlinelen=self._maxheaderlen, header_name=h
                             ).encode().replace('\n', self._NL)
                print >> self._fp, hdr + self._NL,
        # A blank line always separates headers from body
        print >> self._fp, self._NL,

    #
    # Handlers for writing types and subtypes
    #

    def _handle_text(self, msg):
        payload = msg.get_payload()
        if payload is None:
            return
        if not isinstance(payload, basestring):
            raise TypeError('string payload expected: {0!s}'.format(type(payload)))
        if self._mangle_from_:
            payload = fcre.sub('>From ', payload)
        self._fp.write(payload)

    # Default body handler
    _writeBody = _handle_text

    def _handle_multipart(self, msg):
        # The trick here is to write out each part separately, merge them all
        # together, and then make sure that the boundary we've chosen isn't
        # present in the payload.
        msgtexts = []
        subparts = msg.get_payload()
        if subparts is None:
            subparts = []
        elif isinstance(subparts, basestring):
            # e.g. a non-strict parse of a message with no starting boundary.
            self._fp.write(subparts)
            return
        elif not isinstance(subparts, list):
            # Scalar payload
            subparts = [subparts]
        for part in subparts:
            s = StringIO()
            g = self.clone(s)
            g.flatten(part, unixfrom=False, linesep=self._NL)
            msgtexts.append(s.getvalue())
        # BAW: What about boundaries that are wrapped in double-quotes?
        boundary = msg.get_boundary()
        if not boundary:
            # Create a boundary that doesn't appear in any of the
            # message texts.
            alltext = self._NL.join(msgtexts)
            boundary = _make_boundary(alltext)
            msg.set_boundary(boundary)
        # If there's a preamble, write it out, with a trailing CRLF
        if msg.preamble is not None:
            if self._mangle_from_:
                preamble = fcre.sub('>From ', msg.preamble)
            else:
                preamble = msg.preamble
            print >> self._fp, preamble + self._NL,
        # dash-boundary transport-padding CRLF
        print >> self._fp, '--' + boundary + self._NL,
        # body-part
        if msgtexts:
            self._fp.write(msgtexts.pop(0))
        # *encapsulation
        # --> delimiter transport-padding
        # --> CRLF body-part
        for body_part in msgtexts:
            # delimiter transport-padding CRLF
            print >> self._fp, self._NL + '--' + boundary + self._NL,
            # body-part
            self._fp.write(body_part)
        # close-delimiter transport-padding
        self._fp.write(self._NL + '--' + boundary + '--')
        if msg.epilogue is not None:
            print >> self._fp, self._NL,
            if self._mangle_from_:
                epilogue = fcre.sub('>From ', msg.epilogue)
            else:
                epilogue = msg.epilogue
            self._fp.write(epilogue)

    def _handle_multipart_signed(self, msg):
        # The contents of signed parts has to stay unmodified in order to keep
        # the signature intact per RFC1847 2.1, so we disable header wrapping.
        # RDM: This isn't enough to completely preserve the part, but it helps.
        # BRE: Disabled! We are using this to generate the stuff we sign, so
        #      we actually want the logic UNCHANGED.
        return self._handle_multipart(msg)
        # Disabled the following...
        old_maxheaderlen = self._maxheaderlen
        try:
            self._maxheaderlen = 0
            self._handle_multipart(msg)
        finally:
            self._maxheaderlen = old_maxheaderlen

    def _handle_message_delivery_status(self, msg):
        # We can't just write the headers directly to self's file object
        # because this will leave an extra newline between the last header
        # block and the boundary.  Sigh.
        blocks = []
        for part in msg.get_payload():
            s = StringIO()
            g = self.clone(s)
            g.flatten(part, unixfrom=False, linesep=self._NL)
            text = s.getvalue()
            lines = text.split(self._NL)
            # Strip off the unnecessary trailing empty line
            if lines and lines[-1] == '':
                blocks.append(self._NL.join(lines[:-1]))
            else:
                blocks.append(text)
        # Now join all the blocks with an empty line.  This has the lovely
        # effect of separating each block with an empty line, but not adding
        # an extra one after the last one.
        self._fp.write(self._NL.join(blocks))

    def _handle_message(self, msg):
        s = StringIO()
        g = self.clone(s)
        # The payload of a message/rfc822 part should be a multipart sequence
        # of length 1.  The zeroth element of the list should be the Message
        # object for the subpart.  Extract that object, stringify it, and
        # write it out.
        # Except, it turns out, when it's a string instead, which happens when
        # and only when HeaderParser is used on a message of mime type
        # message/rfc822.  Such messages are generated by, for example,
        # Groupwise when forwarding unadorned messages.  (Issue 7970.)  So
        # in that case we just emit the string body.
        payload = msg.get_payload()
        if isinstance(payload, list):
            g.flatten(msg.get_payload(0), unixfrom=False, linesep=self._NL)
            payload = s.getvalue()
        self._fp.write(payload)


_FMT = '[Non-text (%(type)s) part of message omitted, filename %(filename)s]'


class DecodedGenerator(Generator):
    """Generates a text representation of a message.

    Like the Generator base class, except that non-text parts are substituted
    with a format string representing the part.
    """
    def __init__(self, outfp,
                 mangle_from_=True, maxheaderlen=78, fmt=None, linesep=None):
        """Like Generator.__init__() except that an additional optional
        argument is allowed.

        Walks through all subparts of a message.  If the subpart is of main
        type `text', then it prints the decoded payload of the subpart.

        Otherwise, fmt is a format string that is used instead of the message
        payload.  fmt is expanded with the following keywords (in
        %(keyword)s format):

        type       : Full MIME type of the non-text part
        maintype   : Main MIME type of the non-text part
        subtype    : Sub-MIME type of the non-text part
        filename   : Filename of the non-text part
        description: Description associated with the non-text part
        encoding   : Content transfer encoding of the non-text part

        The default value for fmt is None, meaning

        [Non-text (%(type)s) part of message omitted, filename %(filename)s]
        """
        Generator.__init__(self, outfp, mangle_from_, maxheaderlen, linesep)
        if fmt is None:
            self._fmt = _FMT
        else:
            self._fmt = fmt

    def _dispatch(self, msg):
        for part in msg.walk():
            maintype = part.get_content_maintype()
            if maintype == 'text':
                print >> self, part.get_payload(decode=True) + self._NL,
            elif maintype == 'multipart':
                # Just skip this
                pass
            else:
                print >> self, self._fmt % {
                    'type': part.get_content_type(),
                    'maintype': part.get_content_maintype(),
                    'subtype': part.get_content_subtype(),
                    'filename': part.get_filename('[no filename]'),
                    'description': part.get('Content-Description',
                                            '[no description]'),
                    'encoding': part.get('Content-Transfer-Encoding',
                                         '[no encoding]'),
                    } + self._NL,


# Helper
_width = len(repr(sys.maxint-1))
_fmt = '%0{0:d}d'.format(_width)


def _make_boundary(text=None):
    # Craft a random boundary.  If text is given, ensure that the chosen
    # boundary doesn't appear in the text.
    token = random.randrange(sys.maxint)
    boundary = ('=' * 15) + (_fmt % token) + '=='
    if text is None:
        return boundary
    b = boundary
    counter = 0
    while True:
        cre = re.compile('^--' + re.escape(b) + '(--)?$', re.MULTILINE)
        if not cre.search(text):
            break
        b = boundary + '.' + str(counter)
        counter += 1
    return b
