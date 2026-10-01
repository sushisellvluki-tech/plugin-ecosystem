"""Configured credentials only. No public token minting or unverified JWT claims."""
from dataclasses import dataclass
import hashlib
import hmac
import math
import time
import re
from urllib.parse import urlsplit
import jwt


@dataclass(frozen=True)
class Identity:
    organization_id: str
    actor_id: str
    scopes: frozenset[str]

    def context(self):
        return {'organization_id':self.organization_id,'actor_id':self.actor_id,
                '_token_scopes':sorted(self.scopes)}


def identity(claims):
    if not isinstance(claims,dict) or any(not isinstance(claims.get(k),str) or not claims[k] for k in ('organization_id','sub')):
        raise ValueError('Invalid identity')
    scopes=claims.get('scope')
    if not isinstance(scopes,str) or len(scopes)>4096:
        raise ValueError('Invalid scopes')
    if any(not re.fullmatch(r'[a-z][a-z0-9_]*:[a-z][a-z0-9_]*',s) for s in scopes.split()):
        raise ValueError('Invalid scopes')
    return Identity(claims['organization_id'],claims['sub'],frozenset(scopes.split()))


class TokenVerifier:
    def __init__(self, service_keys=(), *, issuer=None, audience=None, public_key=None):
        self.service_keys=[]
        for key in service_keys:
            digest=key.get('sha256','')
            expiry=key.get('expires_at')
            if not re.fullmatch(r'[a-f0-9]{64}',digest) or type(expiry) not in (int,float) or not math.isfinite(expiry):
                raise ValueError('Credential requires SHA-256 and expiry')
            self.service_keys.append((digest,expiry,identity(key)))
        if len({x[0] for x in self.service_keys})!=len(self.service_keys):
            raise ValueError('Duplicate credential hash')
        if any((issuer,audience,public_key)) and not all((issuer,audience,public_key)):
            raise ValueError('JWT issuer, audience and public key must be set together')
        if issuer and (urlsplit(issuer).scheme!='https' or not urlsplit(issuer).netloc):
            raise ValueError('OAuth issuer must use HTTPS')
        self.issuer,self.audience,self.public_key=issuer,audience,public_key
        if not self.service_keys and not issuer:
            raise ValueError('At least one authentication method is required')

    def verify(self,token):
        if not isinstance(token,str) or not 1<=len(token)<=8192:
            raise ValueError('Invalid token')
        digest=hashlib.sha256(token.encode()).hexdigest()
        for expected,expiry,principal in self.service_keys:
            if hmac.compare_digest(expected,digest):
                if expiry<=time.time():
                    raise ValueError('Expired token')
                return principal
        if not self.public_key:
            raise ValueError('Unknown token')
        try:
            claims=jwt.decode(token,self.public_key,algorithms=['RS256'],issuer=self.issuer,
                              audience=self.audience,options={'require':['exp','iat','iss','aud','sub','organization_id','scope']})
            return identity(claims)
        except (jwt.PyJWTError,ValueError,TypeError):
            raise ValueError('Invalid token') from None
