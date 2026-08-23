data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

resource "aws_security_group" "probe" {
  name_prefix = "${local.name_prefix}-probe-"
  description = "Egress-only probe host for SSM; no ingress."
  vpc_id      = data.terraform_remote_state.vpc.outputs.vpc_id

  egress {
    description = "All outbound (SSM/API)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.name_prefix}-probe-sg"
  }

  depends_on = [terraform_data.account_guard]
}

resource "aws_iam_role" "probe" {
  name = "${local.name_prefix}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })

  tags = {
    Name = "${local.name_prefix}-role"
  }

  depends_on = [terraform_data.account_guard]
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.probe.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "probe" {
  name = "${local.name_prefix}-profile"
  role = aws_iam_role.probe.name

  tags = {
    Name = "${local.name_prefix}-profile"
  }
}

resource "aws_instance" "probe" {
  ami                         = data.aws_ssm_parameter.al2023.value
  instance_type               = "t3.nano"
  subnet_id                   = data.terraform_remote_state.vpc.outputs.public_subnet_id
  associate_public_ip_address = true
  vpc_security_group_ids      = [aws_security_group.probe.id]
  iam_instance_profile        = aws_iam_instance_profile.probe.name

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 8
    encrypted             = true
    delete_on_termination = true
  }

  user_data = <<-EOF
    #!/usr/bin/env bash
    set -euo pipefail
    systemctl enable --now amazon-ssm-agent || true
  EOF

  tags = {
    Name = "${local.name_prefix}-probe"
  }

  depends_on = [terraform_data.account_guard]
}
