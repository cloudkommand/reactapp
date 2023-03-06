# reactapp
React UI Plugin(s) for CloudKommand. Deploy React Apps.

There are three broad patterns for using this extension. Production, Personal Website, and Development. Switching between them is generally not recommended, as significant downtime will occur.

## Production (Route53 + Cloudfront + S3)

This pattern deploys React code to an S3 bucket whose content is served exclusively by a Cloudfront Distribution (CDN), with DNS records set by Route53. This is the recommended production stack for using this plugin, and using it properly ensures fully zero-downtime deployments, unless you change the subdomain(s). It is also the only stack that supports multiple subdomains.

## Personal Website (Route53 + S3)

This pattern deploys React code to an S3 bucket with attached DNS records set by Route53. This stack allows you to specify a domain name for your website, and most deployments will result in only 1-3 seconds of downtime. However, this pattern requires the S3 bucket to have a fixed name, so changing the domain name will result in significant downtime. This stack is recommended if you do not want to pay charges for using Cloudfront. https://aws.amazon.com/cloudfront/pricing/

## Development (S3)

This pattern creates a publicly available S3 bucket. This is recommended for development, because it is the simplest to setup and the cheapest. Most deployments will result in only 1-3 seconds of website downtime.
