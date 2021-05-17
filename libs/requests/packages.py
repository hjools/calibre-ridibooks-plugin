import sys

# This code exists for backwards compatibility reasons.
# I don't like it either. Just look the other way. :)

for package in ('calibre_plugins.ridibooks.libs.urllib3', 'calibre_plugins.ridibooks.libs.idna', 'calibre.ebooks.chardet'):
    locals()[package] = __import__(package)
    # This traversal is apparently necessary such that the identities are
    # preserved (requests.packages.urllib3.* is urllib3.*)
    for mod in list(sys.modules):
        if mod == package or mod.startswith(package + '.'):
            sys.modules['requests.packages.' + mod] = sys.modules[mod]

# Kinda cool, though, right?
