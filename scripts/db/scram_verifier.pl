#!/usr/bin/env perl
# Print a PostgreSQL SCRAM-SHA-256 password verifier, computed on the CLIENT (DA-C-3).
#
#   perl scripts/db/scram_verifier.pl <ENV_VAR_NAME>
#
# The password is read from the environment variable NAMED by the argument (never from the
# command line, so it is not visible in ps). Output (RFC 5802 / RFC 7677, PostgreSQL's
# pg_authid format):
#   SCRAM-SHA-256$<iterations>:<salt b64>$<StoredKey b64>:<ServerKey b64>
# `ALTER ROLE … PASSWORD '<verifier>'` stores it as is, so the plaintext never reaches the
# server — nor its logs (log_statement, log_min_error_statement), whatever they are set to.
#
# Perl + Digest::SHA are used because they are in every place apply_roles.sh runs: the
# pgvector/postgres image (compose db-roles, no Python there), WSL and GitHub runners.
# PostgreSQL applies SASLprep, which leaves pure-ASCII passwords unchanged; other passwords
# are refused here instead of being normalised differently from the server.
use strict;
use warnings;
use Digest::SHA qw(hmac_sha256 sha256);
use MIME::Base64 qw(encode_base64);

my $ITERATIONS = 4096;    # PostgreSQL's default scram_iterations
my $SALT_BYTES = 16;      # as PostgreSQL's own SCRAM_DEFAULT_SALT_LEN

@ARGV == 1 && $ARGV[0] =~ /\A[A-Za-z_][A-Za-z0-9_]*\z/
  or die "usage: scram_verifier.pl <ENV_VAR_NAME>\n";
my $name = $ARGV[0];
my $password = $ENV{$name};
defined $password && length $password
  or die "scram_verifier: $name is not set or empty\n";
$password =~ /\A[\x20-\x7e]+\z/
  or die "scram_verifier: $name must be printable ASCII (SASLprep is not implemented)\n";

open my $random, '<:raw', '/dev/urandom' or die "scram_verifier: /dev/urandom: $!\n";
(read($random, my $salt, $SALT_BYTES) // -1) == $SALT_BYTES
  or die "scram_verifier: could not read $SALT_BYTES random bytes\n";
close $random;

# Hi(password, salt, i) = PBKDF2-HMAC-SHA-256 with a single 32-byte block (RFC 5802 §2.2).
my $u = hmac_sha256($salt . pack('N', 1), $password);
my $salted = $u;
for (2 .. $ITERATIONS) {
    $u = hmac_sha256($u, $password);
    $salted ^= $u;
}
my $stored_key = sha256(hmac_sha256('Client Key', $salted));
my $server_key = hmac_sha256('Server Key', $salted);

sub b64 { return encode_base64($_[0], ''); }
printf "SCRAM-SHA-256\$%d:%s\$%s:%s\n", $ITERATIONS, b64($salt), b64($stored_key),
  b64($server_key);
