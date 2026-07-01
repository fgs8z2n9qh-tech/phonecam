"""
Lokalis CA + szerver (leaf) tanusitvany generalasa a HTTPS szerverhez.

Miert kell ez (kulonosen iPhone/Safari miatt):
  A bongeszo a kamerat (getUserMedia) csak "secure context"-ben adja ki. iOS Safari
  egy puszta self-signed cert elfogadasa utan is gyakran megtagadja a kamerat.
  Megbizhato megoldas: sajat CA-t keszitunk, azt telepitjuk + megbizhatova tesszuk
  az iPhone-on (egyszer), a szerver pedig egy ezzel alairt leaf cert-et hasznal.
  iOS 13+ kovetelmenyek: SAN (nem CN), serverAuth EKU, <=398 nap lejarat, SHA-256.

Kimenet (server/ mappaba):
  ca.pem / ca.key       - a CA (a .key maradjon a gepen, soha ne add ki!)
  phonecam-ca.cer       - a CA DER formatumban -> EZT telepited az iPhone-ra
  cert.pem / key.pem    - a szerver leaf cert-je (ezt hasznalja a szerver)

A CA-t csak egyszer generaljuk; ujrafuttataskor megmarad (igy nem kell ujra
telepiteni a telefonra), csak a leaf-et frissitjuk az aktualis IP-kkel.

Futtatas:  python server/make_cert.py
"""
import datetime
import ipaddress
import os
import socket

from cryptography import x509
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

HERE = os.path.dirname(os.path.abspath(__file__))
CA_CERT = os.path.join(HERE, "ca.pem")
CA_KEY = os.path.join(HERE, "ca.key")
CA_DER = os.path.join(HERE, "phonecam-ca.cer")
LEAF_CERT = os.path.join(HERE, "cert.pem")
LEAF_KEY = os.path.join(HERE, "key.pem")


def local_ips():
    ips = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
    except Exception:
        pass
    finally:
        s.close()
    return sorted(ips)


def load_or_make_ca():
    if os.path.exists(CA_CERT) and os.path.exists(CA_KEY):
        with open(CA_KEY, "rb") as f:
            ca_key = serialization.load_pem_private_key(f.read(), password=None)
        with open(CA_CERT, "rb") as f:
            ca_cert = x509.load_pem_x509_certificate(f.read())
        print("Meglevo CA hasznalata (nem kell ujra telepiteni a telefonra).")
        return ca_cert, ca_key

    print("Uj lokalis CA generalasa...")
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PhoneCam Local CA")])
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=False, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True,
            crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    with open(CA_KEY, "wb") as f:
        f.write(ca_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()))
    with open(CA_CERT, "wb") as f:
        f.write(ca_cert.public_bytes(serialization.Encoding.PEM))
    with open(CA_DER, "wb") as f:
        f.write(ca_cert.public_bytes(serialization.Encoding.DER))  # iPhone ezt telepiti
    return ca_cert, ca_key


def make_leaf(ca_cert, ca_key, ips):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    san = [x509.DNSName("localhost"), x509.DNSName("phonecam.local")]   # mDNS-hez (iOS feloldja)
    for ip in ips:
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PhoneCam")]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=397))  # iOS: <= 398 nap
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=True,
            data_encipherment=False, key_agreement=False, key_cert_sign=False,
            crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key()),
            critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    with open(LEAF_KEY, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()))
    with open(LEAF_CERT, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


def main():
    ca_cert, ca_key = load_or_make_ca()
    ips = local_ips()
    make_leaf(ca_cert, ca_key, ips)
    print("Tanusitvanyok keszen:")
    print("   CA (telefonra):", CA_DER)
    print("   szerver leaf:  ", LEAF_CERT)
    print("SAN cimek:", ", ".join(["localhost", "phonecam.local"] + ips))


if __name__ == "__main__":
    main()
