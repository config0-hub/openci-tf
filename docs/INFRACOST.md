# Infracost

Provide the Infracost API key as an SSM parameter reference in repository settings.
The plan runner reads it only while building the encrypted execution package and
renders bounded cost output. Use `just register-repo` as the canonical setup command;
raw Terraform is a fallback.
