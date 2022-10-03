import boto3
import botocore
# import jsonschema
import json
import traceback
import zipfile
import os

from botocore.exceptions import ClientError

from extutil import remove_none_attributes, account_context, ExtensionHandler, ext, \
    current_epoch_time_usec_num, component_safe_name, lambda_env, random_id, \
    handle_common_errors, create_zip

eh = ExtensionHandler()
SUCCESS_FILE = "reactspapresets/success.json"
ERROR_FILE = "reactspapresets/error.json"

cloudfront = boto3.client("cloudfront")

def lambda_handler(event, context):
    try:
        print(f"event = {event}")
        # account_number = account_context(context)['number']
        region = account_context(context)['region']
        eh.capture_event(event)
        bucket = event.get("bucket")
        object_name = event.get("s3_object_name")

        prev_state = event.get("prev_state") or {}
        project_code = event.get("project_code")
        repo_id = event.get("repo_id")
        cdef = event.get("component_def")
        cname = event.get("component_name")
        role_arn = lambda_env("codebuild_role_arn")
        codebuild_project_name = cdef.get("codebuild_project_name") or component_safe_name(project_code, repo_id, cname)
        codebuild_runtime_versions = cdef.get("codebuild_runtime_versions") or {"nodejs": 10} # assume dictionary with this format
        codebuild_install_commands = cdef.get("codebuild_install_commands") or None
        
        caller_reference = str(current_epoch_time_usec_num())
        if not eh.state.get("caller_reference"):
            eh.add_state({"caller_reference": caller_reference})

        build_container_size = cdef.get("build_container_size")
        # s3_url_path = cdef.get("s3_url_path") or "/"
        base_domain_length = len(cdef.get("base_domain")) if cdef.get("base_domain") else 0
        domain = cdef.get("domain") or (form_domain(component_safe_name(project_code, repo_id, cname, no_underscores=True, max_chars=62-base_domain_length), cdef.get("base_domain")) if cdef.get("base_domain") else None)
        domains = cdef.get("domains") or [domain]
        if cdef.get("cloudfront") and not domains:
            eh.add_log("Cloudfront requires at least one domain", {"cdef": cdef}, True)
            eh.perm_error("Cloudfront requires at least one domain", 0)

        index_document = cdef.get("index_document") or "index.html"
        error_document = cdef.get("error_document") or "index.html"
    
        if event.get("pass_back_data"):
            print(f"pass_back_data found")
        elif event.get("op") == "upsert":
            eh.add_op("get_codebuild_state")
            eh.add_op("setup_s3")
            eh.add_op("setup_status_objects")
            eh.add_op("put_object")
            if cdef.get("cloudfront"):
                eh.add_op("setup_cloudfront_distribution")
                eh.add_op("invalidate_files")
                if cdef.get("keep_bucket_private"):
                    eh.add_op("setup_cloudfront_oai")
            if cdef.get("config"):
                eh.add_op("add_config")
            if domain:
                eh.add_op("setup_route53")

        elif event.get("op") == "delete":
            eh.add_op("remove_codebuild_project", {"create_and_remove": False, "name": codebuild_project_name})
            eh.add_props(prev_state.get("props", {}))
            if cdef.get("cloudfront"):
                eh.add_op("setup_cloudfront_distribution")
                if cdef.get("keep_bucket_private"):
                    eh.add_op("setup_cloudfront_oai")
            if domain:
                eh.add_op("setup_route53")

        compare_defs(event)
        compare_etags(event, bucket, object_name)

        load_initial_props(bucket, object_name)

        get_codebuild_state(cname, cdef, codebuild_project_name, prev_state)
        setup_status_objects(bucket)
        add_config(bucket, object_name, cdef.get("config"))
        # put_object(bucket, object_name, s3_build_object_name)
        setup_cloudfront_oai(cdef)
        setup_s3(cname, cdef, domain, index_document, error_document)
        setup_codebuild_project(codebuild_project_name, bucket, object_name, build_container_size, role_arn, prev_state, cname, repo_id, codebuild_runtime_versions, codebuild_install_commands)
        start_build(codebuild_project_name)
        check_build_complete(bucket)
        set_object_metadata(cdef, index_document, error_document, region, domain)
        setup_cloudfront_distribution(cname, cdef, domain, index_document, prev_state)
        
        #Have to do it after CF distribution is gone
        if event["op"] == "delete":
            eh.add_op("setup_s3")
            setup_s3(cname, cdef, domain, index_document, error_document)

        setup_route53(cname, cdef, domain, prev_state)
        remove_codebuild_project()
        invalidate_files()
        # check_invalidation_complete()
            
        return eh.finish()

    except Exception as e:
        msg = traceback.format_exc()
        print(msg)
        eh.add_log("Unexpected Error", {"error": msg}, is_error=True)
        eh.declare_return(200, 0, error_code=str(e))
        return eh.finish()

def get_s3_etag(bucket, object_name):
    s3 = boto3.client("s3")

    try:
        s3_metadata = s3.head_object(Bucket=bucket, Key=object_name)
        print(f"s3_metadata = {s3_metadata}")
        eh.add_state({"zip_etag": s3_metadata['ETag']})
    except s3.exceptions.NoSuchKey:
        eh.add_log("Cound Not Find Zipfile", {"bucket": bucket, "key": object_name})
        eh.retry_error("Object Not Found")

@ext(handler=eh, op="compare_defs")
def compare_defs(event):
    old_rendef = event.get("prev_state", {}).get("rendef")
    new_rendef = event.get("component_def")

    _ = old_rendef.pop("trust_level", None)
    _ = new_rendef.pop("trust_level", None)

    if old_rendef == new_rendef:
        eh.add_op("compare_etags")

    else:
        eh.add_log("Definitions Don't Match, Deploying", {"old": old_rendef, "new": new_rendef})

@ext(handler=eh, op="compare_etags")
def compare_etags(event, bucket, object_name):
    old_props = event.get("prev_state", {}).get("props", {})

    initial_etag = old_props.get("initial_etag")

    #Get new etag
    get_s3_etag(bucket, object_name)
    if eh.state.get("zip_etag"):
        new_etag = eh.state["zip_etag"]
        if initial_etag == new_etag:
            eh.add_log("Elevated Trust: No Change Detected", {"initial_etag": initial_etag, "new_etag": new_etag})
            eh.add_props(old_props)
            eh.add_links(event.get("prev_state", {}).get("links", {}))
            eh.add_state(event.get("prev_state", {}).get("state", {}))
            eh.declare_return(200, 100, success=True)

        else:
            eh.add_log("Code Changed, Deploying", {"old_etag": initial_etag, "new_etag": new_etag})

@ext(handler=eh, op="load_initial_props")
def load_initial_props(bucket, object_name):
    get_s3_etag(bucket, object_name)
    if eh.state.get("zip_etag"):
        eh.add_props({"initial_etag": eh.state.get("zip_etag")})

# def format_tags(tags_dict):
#     return [{"Key": k, "Value": v} for k,v in tags_dict]
@ext(handler=eh, op="get_codebuild_state")
def get_codebuild_state(cname, cdef, codebuild_project_name, prev_state):
    eh.add_op("setup_codebuild_project")
    
    if prev_state and prev_state.get("props") and prev_state.get("props").get("codebuild_project_name"):
        prev_codebuild_project_name = prev_state.get("props").get("codebuild_project_name")
        if codebuild_project_name != prev_codebuild_project_name:
            eh.add_op("remove_codebuild_project", {"create_and_remove": True, "name": prev_codebuild_project_name})


@ext(handler=eh, op="setup_route53")
def setup_route53(cname, cdef, domain, prev_state):
    print(f"props = {eh.props}")
    if cdef.get("cloudfront"):
        component_def = {
            "domain": domain,
            "alias_target_type": "cloudfront",
            "target_cloudfront_domain_name": eh.props["Distribution"]["domain_name"]
        }
    else:
        #  or prev_state.get("rendef", {}).get("S3", {})
        S3 = eh.props.get("S3", {})
        component_def = {
            "target_s3_region": S3.get("region"),
            "target_s3_bucket": S3.get("name")
        }

    function_arn = lambda_env('route53_extension_arn')

    proceed = eh.invoke_extension(
        arn=function_arn, component_def=component_def, 
        child_key="Route53", progress_start=85, progress_end=100,
        merge_props=False)

    if proceed:
        eh.add_links({"Website URL": f'http://{eh.props["Route53"].get("domain")}'})
    print(f"proceed = {proceed}")

@ext(handler=eh, op="setup_cloudfront_oai")
def setup_cloudfront_oai(cdef):
    print(f"props = {eh.props}")
    component_def = remove_none_attributes({
        "existing_id": cdef.get("oai_existing_id")
    })

    function_arn = lambda_env('cloudfront_oai_extension_arn')

    proceed = eh.invoke_extension(
        arn=function_arn, component_def=component_def, 
        child_key="OAI", progress_start=85, progress_end=100,
        merge_props=False)
    print(f"proceed = {proceed}")

@ext(handler=eh, op="setup_cloudfront_distribution")
def setup_cloudfront_distribution(cname, cdef, domain, index_document, prev_state):
    print(f"props = {eh.props}")

    S3 = eh.props.get("S3", {})
    component_def = remove_none_attributes({
        "aliases": [domain],
        "target_s3_bucket": S3.get("name"),
        "default_root_object": index_document,
        "oai_id": eh.props.get("OAI", {}).get("id"),
        "existing_id": cdef.get("cloudfront_existing_id"),
        "origin_shield": cdef.get("cloudfront_origin_shield"),
        "custom_origin_headers": cdef.get("cloudfront_custom_origin_headers"),
        "force_https": cdef.get("cloudfront_force_https"),
        "allowed_ssl_protocols": cdef.get("cloudfront_allowed_ssl_protocols"),
        "price_class": cdef.get("cloudfront_price_class"),
        "web_acl_id": cdef.get("cloudfront_web_acl_id"),
        "logs_s3_bucket": cdef.get("cloudfront_logs_s3_bucket"),
        "logs_include_cookies": cdef.get("cloudfront_logs_include_cookies"),
        "logs_prefix": cdef.get("cloudfront_logs_prefix"),
        "key_group_ids": cdef.get("cloudfront_key_group_ids"),
        "allowed_methods": cdef.get("cloudfront_allowed_methods") or ["HEAD", "GET", "OPTIONS"],
        "cached_methods": cdef.get("cloudfront_cached_methods") or ["HEAD", "GET", "OPTIONS"],
        "cache_policy_id": cdef.get("cloudfront_cache_policy_id"),
        "cache_policy_name": cdef.get("cloudfront_cache_policy_name"),
        "tags": cdef.get("cloudfront_tags")
    })

    function_arn = lambda_env('cloudfront_distribution_extension_arn')

    proceed = eh.invoke_extension(
        arn=function_arn, component_def=component_def, 
        child_key="Distribution", progress_start=85, progress_end=100,
        merge_props=False)
    print(f"proceed = {proceed}")
        
@ext(handler=eh, op="setup_s3")
def setup_s3(cname, cdef, domain, index_document, error_document):
    # l_client = boto3.client('lambda')

    website_configuration = None
    block_public_access = True
    # public_access_block = None
    acl = None
    if cdef.get("cloudfront") and cdef.get("keep_bucket_private"):
        bucket_policy = {
            "Version": "2012-10-17",
            "Id": "BucketPolicyCloudfront",
            "Statement": [
                {
                    "Sid": "AllowCloudfront",
                    "Effect": "Allow",
                    "Principal": {
                        "AWS": eh.props["OAI"]['arn']
                    },
                    "Action": "s3:GetObject",
                    "Resource": "$SELF$/*"
                }
            ]
        }
    # elif cdef.get("base_domain"):
    #     bucket_policy = "TBD"
    else: #No Cloudfront
        bucket_policy = {
            "Version": "2012-10-17",
            "Id": "BucketPolicy",
            "Statement": [
                {
                    "Sid": "PublicReadForGetBucketObjects",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": "$SELF$/*"
                }
            ]
        }
        website_configuration = {
            "error_document": error_document,
            "index_document": index_document
        }
        block_public_access = False
        acl = {
            "GrantRead": "uri=http://acs.amazonaws.com/groups/global/AllUsers"
        }

    function_arn = lambda_env('s3_extension_arn')
    component_def = remove_none_attributes({
        # "CORS": True,
        "name": domain,
        "website_configuration": website_configuration,
        "bucket_policy": bucket_policy,
        "block_public_access": block_public_access,
        "acl": acl,
        "tags": cdef.get("s3_tags")
    })

    proceed = eh.invoke_extension(
        arn=function_arn, component_def=component_def, 
        child_key="S3", progress_start=20, progress_end=50,
        merge_props=False)
    print(f"proceed = {proceed}")


@ext(handler=eh, op="add_config")
def add_config(bucket, object_name, config):
    s3 = boto3.client("s3")
    print(f"add_config")

    try:
        filename = f"/tmp/{random_id()}.zip"
        with open(filename, "wb") as f:
            s3.download_fileobj(bucket, object_name, f)
            # f.write(response['Body'])
    except ClientError as e:
        handle_common_errors(e, eh, "Downloading Zipfile Failed", 15)
        return 0

    directory = f"/tmp/{random_id()}"
    os.makedirs(directory)
    with zipfile.ZipFile(filename, 'r') as archive:
        archive.extractall(path=directory)

    filepath = config.get("filepath") or 'src/config/config.js'
    data = config['data']

    path_to_write = f"{directory}/{filepath}"
    os.makedirs(path_to_write.rsplit('/', 1)[0], exist_ok=True)

    with open(path_to_write, 'w') as g:
        content = f"""
            export function get_config() {{
                return {json.dumps(data, indent=2)}
            }};
        """
        g.write(content)

    filename2 = f"/tmp/{random_id()}.zip"
    create_zip(filename2, directory)

    try:
        response = s3.upload_file(filename2, bucket, object_name)
    except ClientError as e:
        handle_common_errors(e, eh, "Reuploading Zipfile Failed", 15)

    eh.add_log("Added Config", {"config": config, "filestr": content})

# @ext(handler=eh, op="put_object")
# def put_object(bucket, object_name, s3_build_object_name):
#     s3 = boto3.client("s3")

#     try:
#         response = s3.copy_object(
#             Bucket=bucket,
#             Key=s3_build_object_name,
#             CopySource=f"{bucket}/{object_name}"
#             # ,
#             # MetadataDirective="REPLACE",
#             # CacheControl="max-age=0",
#             # ContentType="text/html"
#         )
#         print(f"copy_object response = {response}")
#     except ClientError as e:
#         handle_common_errors(e, eh, "Copying Zipfile Failed", 15)

@ext(handler=eh, op="setup_status_objects")
def setup_status_objects(bucket):
    s3 = boto3.client("s3")
    print(f"setup_status_objects")

    try:
        response = s3.get_object(Bucket=bucket, Key=ERROR_FILE)
        eh.add_log("Status Objects Exist", {"bucket": bucket, "success": SUCCESS_FILE, "error": ERROR_FILE})
    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] == "NoSuchKey":
            try:
                success = {"value": "success"}
                s3.put_object(
                    Body=json.dumps(success),
                    Bucket=bucket,
                    Key=SUCCESS_FILE
                )

                error = {"value": "error"}
                s3.put_object(
                    Body=json.dumps(error),
                    Bucket=bucket,
                    Key=ERROR_FILE
                )
        
                eh.add_log("Status Objects Created", {"bucket": bucket, "success": SUCCESS_FILE, "error": ERROR_FILE})
            except:
                eh.add_log("Error Writing Status Objects", {"error": str(e)}, True)
                eh.retry_error(str(e), 10)

        else:
            eh.add_log("Error Getting Status Object", {"error": str(e)}, True)
            eh.retry_error(str(e), 10)


@ext(handler=eh, op="setup_codebuild_project")
def setup_codebuild_project(codebuild_project_name, bucket, object_name, build_container_size, role_arn, prev_state, component_name, repo_id, codebuild_runtime_versions, codebuild_install_commands):
    codebuild = boto3.client('codebuild')
    destination_bucket = eh.props['S3']['name']
    pre_build_commands = []

    # pre_build_commands = ["npm install -g react-scripts"]
    # if bundler_name == "webpack":
    #     pass
    # elif bundler_name:
    #     pre_build_commands.extend([f"npm install -g {bundler_name}"])
    # else:
    #     pre_build_commands.extend([
    #         "npm install -g webpack",
    #         "npm install -g vite",
    #         "npm install -g browserify",
    #         "npm install -g esbuild",
    #         "npm install -g rollup",
    #         "npm install -g parcel"
    #     ])

    if build_container_size:
        if isinstance(build_container_size, str):
            if build_container_size.lower() == "small":
                build_container_size = "BUILD_GENERAL1_SMALL"
            elif build_container_size.lower() == "medium":
                build_container_size = "BUILD_GENERAL1_MEDIUM"
            elif build_container_size.lower() == "large":
                build_container_size = "BUILD_GENERAL1_LARGE"
            elif (build_container_size.lower() == "2xlarge") or (build_container_size.lower() == "xxlarge"):
                build_container_size = "BUILD_GENERAL1_2XLARGE"
            elif build_container_size in ["BUILD_GENERAL1_SMALL", "BUILD_GENERAL1_MEDIUM", "BUILD_GENERAL1_LARGE", "BUILD_GENERAL1_2XLARGE"]:
                pass
            else:
                eh.add_log("Invalid build_container_size, using MEDIUM", {"build_container_size": build_container_size})
                build_container_size = "BUILD_GENERAL1_MEDIUM"
        else:
            if build_container_size == 1:
                build_container_size = "BUILD_GENERAL1_SMALL"
            elif build_container_size == 2:
                build_container_size = "BUILD_GENERAL1_MEDIUM"
            elif build_container_size == 3:
                build_container_size = "BUILD_GENERAL1_LARGE"
            elif build_container_size == 4:
                build_container_size = "BUILD_GENERAL1_2XLARGE"
            else:
                eh.add_log("Invalid build_container_size, using MEDIUM", {"build_container_size": build_container_size})
                build_container_size = "BUILD_GENERAL1_MEDIUM"
    else:
        build_container_size = "BUILD_GENERAL1_MEDIUM"

    try:
        params = {
            "name": codebuild_project_name,
            "description": f"Codebuild project for component {component_name} in app {repo_id}",
            "source": {
                "type": "S3",
                "location": f"{bucket}/{object_name}",
                "buildspec": json.dumps({
                    "version": 0.2,
                    "env": {
                        "variables": {
                            "THIS_BUILD_KEY": "whocares"
                        }
                    },
                    "phases": remove_none_attributes({
                        "install": remove_none_attributes({
                            "runtime-versions": codebuild_runtime_versions,
                            "commands": codebuild_install_commands or None
                        }) or None,
                        "pre_build": remove_none_attributes({
                            "commands": pre_build_commands or None
                        }) or None,
                        "build": {
                            "commands": [
                                "mkdir -p build",
                                "npm install",
                                "npm run build"
                            ]
                        },
                        "post_build": {
                            "commands": [
                                f'bash -c "if [ \"$CODEBUILD_BUILD_SUCCEEDING\" == \"1\" ]; then aws s3 cp s3://{bucket}/{SUCCESS_FILE} s3://{bucket}/$THIS_BUILD_KEY; else aws s3 cp s3://{bucket}/{ERROR_FILE} s3://{bucket}/$THIS_BUILD_KEY; fi"'
                            ]
                        }
                    }), 
                    "artifacts": {
                        "files": [
                            "**/*"
                        ],
                        "base-directory": "build"
                    }
                }, sort_keys=True)
            },
            "artifacts": {
                "type": "S3",
                "location": destination_bucket,
                "path": "/",
                "namespaceType": "NONE",
                "name": "/",
                "packaging": "NONE",
                "encryptionDisabled": True
            },
            "environment": {
                "type": "LINUX_CONTAINER",
                "image": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
                "computeType": build_container_size,
                "imagePullCredentialsType": "CODEBUILD"
            },
            "serviceRole": role_arn
        }
        print(f"params = {params}")
        this_params_hash = json.dumps(params, sort_keys=True)

        response = codebuild.create_project(**params).get("project")
        eh.add_log("Created Codebuild Project", response)
        eh.add_props({
            "codebuild_project_arn": response['arn'],
            "codebuild_project_name": response['name'],
            "hash": this_params_hash
        })
        eh.add_op("start_build")
        eh.add_links({"Codebuild Project": gen_codebuild_link(codebuild_project_name)})
    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] == "ResourceAlreadyExistsException":
            try:
                # if this_params_hash != prev_state.get("props", {}).get("hash"):
                response = codebuild.update_project(**params).get("project")
                eh.add_log("Updated Codebuild Project", response)
                eh.add_props({
                    "codebuild_project_arn": response['arn'],
                    "codebuild_project_name": response['name'],
                    "hash": json.dumps(params, sort_keys=True)
                })
                eh.add_op("start_build")
                eh.add_links({"Codebuild Project": gen_codebuild_link(codebuild_project_name)})
                
            except botocore.exceptions.ClientError as e:
                if e.response['Error']['Code'] == "InvalidInputException":
                    eh.add_log("Invalid Codebuild Input", {"error": str(e)}, True)
                    eh.perm_error("Invalid Codebuild Input", 50)
                elif e.response['Error']['Code'] == "ResourceNotFoundException":
                    eh.add_log("Codebuild Project Gone", {"error": str(e)}, True)
                    eh.perm_error("Codebuild Project Gone", 50)
                else:
                    eh.add_log("Codebuild Error", {"error": str(e)}, True)
                    eh.retry_error(str(e), 50)

        elif e.response['Error']['Code'] == "InvalidInputException":
            eh.add_log("Invalid Codebuild Input", {"error": str(e)}, True)
            eh.perm_error("Invalid Codebuild Input", 50)
        elif e.response['Error']['Code'] == "AccountLimitExceededException":
            eh.add_log("Codebuild Limit Excceeded", {"error": str(e)}, True)
            eh.perm_error("Codebuild Limit Excceeded", 50)
        else:
            eh.add_log("Codebuild Error", {"error": str(e)}, True)
            eh.retry_error(str(e), 50)


@ext(handler=eh, op="remove_codebuild_project")
def remove_codebuild_project():
    codebuild = boto3.client('codebuild')

    codebuild_project_name = eh.ops['remove_codebuild_project'].get("name")
    car = eh.ops['remove_codebuild_project'].get("create_and_remove")

    try:
        _ = codebuild.delete_project(name=codebuild_project_name)
        eh.add_log("Deleted Project if it Existed", {"name": codebuild_project_name})
    except botocore.exceptions.ClientError as e:
        eh.add_log("Remove Codebuild Error", {"error": str(e)}, True)
        eh.retry_error(str(e), 60 if car else 15)

@ext(handler=eh, op="start_build")
def start_build(codebuild_project_name):
    codebuild = boto3.client('codebuild')
    this_build_key = f"reactspabuilds/{random_id()}.json"

    try:
        response = codebuild.start_build(
            projectName=codebuild_project_name,
            environmentVariablesOverride=[
                {
                    "name": "THIS_BUILD_KEY",
                    "value": this_build_key,
                    "type": "PLAINTEXT"
                }
            ]).get("build")
        # eh.add_state({"codebuild_id": response.get("id")})
        eh.add_log("Start Build", response)
        # eh.add_state({"this_build_key": this_build_key})
        eh.add_op("check_build_complete", this_build_key)
    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] in ['InvalidInputException', 'ResourceNotFoundException']:
            eh.add_log("Start Build Failed", {"error": str(e)}, True)
            eh.perm_error(str(e), progress=50)
        else:
            eh.add_log("Start Build Error", {"error": str(e)}, True)
            eh.retry_error(str(e), progress=50)

@ext(handler=eh, op="check_build_complete")
def check_build_complete(bucket):
    s3 = boto3.client("s3")

    build_key = eh.ops['check_build_complete']
    print(f'build_key = {build_key}')
    print(f"bucket = {bucket}")
    
    try:
        response = s3.get_object(Bucket=bucket, Key=build_key)['Body']
        value = json.loads(response.read()).get("value")
        if value == "success":
            eh.add_log("Build Succeeded", response)
            eh.add_op("set_object_metadata")
            return None
        else:
            eh.add_log(f"End Build: error", response)
            eh.perm_error(f"End Build: error", progress=65)

    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] in ['NoSuchKey']:
            eh.add_log("Build In Progress", {"error": None})
            eh.retry_error(str(current_epoch_time_usec_num()), progress=65, callback_sec=8)
            # eh.add_log("Check Build Failed", {"error": str(e)}, True)
            # eh.perm_error(str(e), progress=65)
        else:
            eh.add_log("Check Build Error", {"error": str(e)}, True)
            eh.retry_error(str(e), progress=65)


    # try:
    #     response = codebuild.batch_get_builds(ids=[build_id])
    #     if response.get("buildsNotFound"):
    #         eh.add_log("Could Not Find Build", {"build_id": build_id}, True)
    #         eh.perm_error("Build Not Found", progress=65)
    #     else:
    #         build_status = response.get("builds")[0].get("buildStatus")
    #         if build_status == "SUCCEEDED":
    #             eh.add_log("Build Succeeded", response)
    #             eh.add_op("set_object_metadata")
    #             return None
    #         elif build_status == "IN_PROGRESS":
    #             eh.add_log("Build In Progress", response)
    #             eh.retry_error(str(current_epoch_time_usec_num()), progress=65, callback_sec=7)
    #         else:
    #             eh.add_log(f"End Build: {build_status}", response)
    #             eh.perm_error(f"End Build: {build_status}", progress=65)

    
    
@ext(handler=eh, op="set_object_metadata")
def set_object_metadata(cdef, index_document, error_document, region, domain):
    s3 = boto3.client('s3')

    bucket_name = eh.props['S3']['name']
    key = index_document
    print(f"bucket_name = {bucket_name}")
    print(f"key = {key}")
    # print(f"s3_url_path = {s3_url_path}")

    try:
        response = s3.copy_object(
            Bucket=bucket_name,
            Key=key,
            CopySource=f"{bucket_name}/{key}",
            MetadataDirective="REPLACE",
            CacheControl="max-age=0",
            ContentType="text/html"
        )
        eh.add_log(f"Fixed {index_document}", response)

        if error_document != index_document:
            key = error_document
            response = s3.copy_object(
                Bucket=bucket_name,
                Key=key,
                CopySource=f"{bucket_name}/{key}",
                MetadataDirective="REPLACE",
                CacheControl="max-age=0",
                ContentType="text/html"
            )
            eh.add_log(f"Fixed {error_document}", response)

        if (not cdef.get("cloudfront")) and (not domain):
            eh.add_links({"Website URL": gen_s3_url(bucket_name, "/", region)})
    except botocore.exceptions.ClientError as e:
        eh.add_log("Error setting Object Metadata", {"error": str(e)}, True)
        eh.retry_error(str(e), 95 if not domain else 85)

#Note that invalidate files and checking for it should really be its own plugin.
@ext(handler=eh, op="invalidate_files")
def invalidate_files():
    distribution_id = eh.props['Distribution']['id']

    try:
        response = cloudfront.create_invalidation(
            DistributionId=distribution_id,
            InvalidationBatch={
                'Paths': {
                    'Quantity': 1,
                    'Items': ['/*']
                },
                'CallerReference': eh.state["caller_reference"]
            }
        )
        
        invalidation = response.get("Invalidation")
        if invalidation.get("Status") != "Completed":
            # eh.add_op("check_invalidation_complete", invalidation.get("Id"))
            eh.add_log("Initiated Cloudfront File Reset", response)
        else:
            eh.add_log("Cloudfront File Reset Complete", response)

    except botocore.exceptions.ClientError as e:
        handle_common_errors(e, eh, "Resetting Cloudfront Files Error", 97)

# @ext(handler=eh, op="check_invalidation_complete")
# def check_invalidation_complete():
#     distribution_id = eh.props['Distribution']['id']
#     invalidation_id = eh.ops['check_invalidation_complete']

#     try:
#         response = cloudfront.get_invalidation(
#             DistributionId=distribution_id,
#             Id=invalidation_id
#         )
        
#         invalidation = response.get("Invalidation")
#         if invalidation.get("Status") != "Completed":
#             eh.add_log("Cloudfront Files Not Yet Reset", response)
#             eh.retry_error(str(current_epoch_time_usec_num()), progress=98, callback_sec=7)
#         else:
#             eh.add_log("Cloudfront Files Have Reset", response)

#     except botocore.exceptions.ClientError as e:
#         handle_common_errors(e, eh, "Check Cloudfront Reset Error", 98)

# http://ck-azra-web-bucket.s3-website-us-east-1.amazonaws.com/login 
def gen_s3_url(bucket_name, s3_url_path, region):
    return f'http://{bucket_name}.s3-website-{region}.amazonaws.com{s3_url_path if s3_url_path != "/" else ""}'

def gen_codebuild_link(codebuild_project_name):
    return f"https://console.aws.amazon.com/codesuite/codebuild/projects/{codebuild_project_name}"

def form_domain(bucket, base_domain):
    if bucket and base_domain:
        return f"{bucket}.{base_domain}"
    else:
        return None
# @ext(handler=eh, op="get_codebuild_project")
# def get_codebuild_project():
#     codebuild = boto3.client('codebuild')

#     codebuild_project_name = eh.ops['remove_codebuild_project'].get("name")
#     car = eh.ops['remove_codebuild_project'].get("create_and_remove")

#     try:
#         response = codebuild.batch_get_projects(names=[codebuild_project_name])
#         if response.get("projects")[0]:
#             eh.add_log("Found Codebuild Project", )
#     except botocore.exceptions.ClientError as e:
#         eh.add_log("Remove Codebuild Error", {"error": str(e)}, True)
#         eh.retry_error(str(e), 60 if car else 15)

"""
aws/codebuild/amazonlinux2-x86_64-standard:3.0	
AMAZON LINUX 2 AVAILABILITY:
version: 0.1

runtimes:
  android:
    versions:
      28:
        requires:
          java: ["corretto8"]
        commands:
          - echo "Installing Android version 28 ..."
      29:
        requires:
          java: ["corretto8"]
        commands:
          - echo "Installing Android version 29 ..."
  java:
    versions:
      corretto11:
        commands:
          - echo "Installing corretto(OpenJDK) version 11 ..."

          - export JAVA_HOME="$JAVA_11_HOME"

          - export JRE_HOME="$JRE_11_HOME"

          - export JDK_HOME="$JDK_11_HOME"

          - |-
            for tool_path in "$JAVA_HOME"/bin/*;
             do tool=`basename "$tool_path"`;
              if [ $tool != 'java-rmi.cgi' ];
              then
               rm -f /usr/bin/$tool /var/lib/alternatives/$tool \
                && update-alternatives --install /usr/bin/$tool $tool $tool_path 20000;
              fi;
            done
      corretto8:
        commands:
          - echo "Installing corretto(OpenJDK) version 8 ..."

          - export JAVA_HOME="$JAVA_8_HOME"

          - export JRE_HOME="$JRE_8_HOME"

          - export JDK_HOME="$JDK_8_HOME"

          - |-
            for tool_path in "$JAVA_8_HOME"/bin/* "$JRE_8_HOME"/bin/*;
             do tool=`basename "$tool_path"`;
              if [ $tool != 'java-rmi.cgi' ];
              then
               rm -f /usr/bin/$tool /var/lib/alternatives/$tool \
                && update-alternatives --install /usr/bin/$tool $tool $tool_path 20000;
              fi;
            done
  golang:
    versions:
      1.12:
        commands:
          - echo "Installing Go version 1.12 ..."
          - goenv global  $GOLANG_12_VERSION
      1.13:
        commands:
          - echo "Installing Go version 1.13 ..."
          - goenv global  $GOLANG_13_VERSION
      1.14:
        commands:
          - echo "Installing Go version 1.14 ..."
          - goenv global  $GOLANG_14_VERSION
  python:
    versions:
      3.9:
        commands:
          - echo "Installing Python version 3.9 ..."
          - pyenv global  $PYTHON_39_VERSION
      3.8:
        commands:
          - echo "Installing Python version 3.8 ..."
          - pyenv global  $PYTHON_38_VERSION
      3.7:
        commands:
          - echo "Installing Python version 3.7 ..."
          - pyenv global  $PYTHON_37_VERSION
  php:
    versions:
      7.4:
        commands:
          - echo "Installing PHP version 7.4 ..."
          - phpenv global $PHP_74_VERSION
      7.3:
        commands:
          - echo "Installing PHP version 7.3 ..."
          - phpenv global $PHP_73_VERSION
  ruby:
    versions:
      2.6:
        commands:
          - echo "Installing Ruby version 2.6 ..."
          - rbenv global $RUBY_26_VERSION
      2.7:
        commands:
          - echo "Installing Ruby version 2.7 ..."
          - rbenv global $RUBY_27_VERSION
  nodejs:
    versions:
      10:
        commands:
          - echo "Installing Node.js version 10 ..."
          - n $NODE_10_VERSION
      12:
        commands:
          - echo "Installing Node.js version 12 ..."
          - n $NODE_12_VERSION
  docker:
    versions:
      18:
        commands:
          - echo "Using Docker 19"
      19:
        commands:
          - echo "Using Docker 19"
  dotnet:
    versions:
      3.1:
        commands:
          - echo "Installing .NET version 3.1 ..."
"""

    

