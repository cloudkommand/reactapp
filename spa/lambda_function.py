import boto3
import botocore
# import jsonschema
import json
import traceback
import zipfile
import os
import io
import mimetypes

from botocore.exceptions import ClientError

from extutil import remove_none_attributes, account_context, ExtensionHandler, ext, \
    current_epoch_time_usec_num, component_safe_name, lambda_env, random_id, \
    handle_common_errors, create_zip

eh = ExtensionHandler()
SUCCESS_FILE = "reactspapresets/success.json"
ERROR_FILE = "reactspapresets/error.json"

SOLO_KEY = "1solo1"
CODEBUILD_PROJECT_KEY = "Codebuild Project"
CODEBUILD_BUILD_KEY = "Codebuild Build"
CLOUDFRONT_OAI_KEY = "OAI"
CLOUDFRONT_DISTRIBUTION_KEY = "Distribution"
S3_KEY = "S3"

cloudfront = boto3.client("cloudfront")
s3 = boto3.client('s3')


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
        trust_level = cdef.get("trust_level") or "code"

        # codebuild_project_name = cdef.get("codebuild_project_name") or component_safe_name(project_code, repo_id, cname)
        # codebuild_runtime_versions = cdef.get("codebuild_runtime_versions") or {"nodejs": 10} # assume dictionary with this format
        # codebuild_install_commands = cdef.get("codebuild_install_commands") or None
        # codebuild_build_commands = cdef.get("codebuild_build_commands") or [ "mkdir -p build", "npm install", "npm run build" ]
        
        codebuild_project_override_def = cdef.get(CODEBUILD_PROJECT_KEY) or {} #For codebuild project overrides
        codebuild_build_override_def = cdef.get(CODEBUILD_BUILD_KEY) or {} #For codebuild build overrides

        cloudfront_distribution_override_def = cdef.get(CLOUDFRONT_DISTRIBUTION_KEY) or {} #For cloudfront distribution overrides

        # s3_override_def = cdef.get(S3_KEY) or {} #For s3 overrides
        oai_override_def = cdef.get(CLOUDFRONT_OAI_KEY) or {} #For cloudfront oai overrides

        # Making sure the Cloudfront distribution thing goes through
        caller_reference = str(current_epoch_time_usec_num())
        if not eh.state.get("caller_reference"):
            eh.add_state({"caller_reference": caller_reference})

        build_container_size = cdef.get("build_container_size")
        node_version = cdef.get("node_version") or 10

        # s3_url_path = cdef.get("s3_url_path") or "/"
        base_domain_length = len(cdef.get("base_domain")) if cdef.get("base_domain") else 0
        domain = cdef.get("domain") or (form_domain(component_safe_name(project_code, repo_id, cname, no_underscores=True, max_chars=62-base_domain_length), cdef.get("base_domain")) if cdef.get("base_domain") else None)
        domains = fix_domains(cdef.get("domains")) or ({SOLO_KEY: {"domain": domain}} if domain else None)
        # If you want to specify a hosted zone for route53, you should set domains to:
        # {
        #     "key": {
        #         "domain": "example.com",
        #         "hosted_zone_id": "Z2FDTNDATAQYW2"
        #     }
        # }
        # Otherwise you can set it to:
        # {
        #    "key": "example.com"
        # }

        cloudfront = cdef.get("cloudfront")
        if domains and not isinstance(domains, dict):
            eh.add_log("domains must be a dictionary", {"domains": domains})
            eh.perm_error("Invalid Domains", 0)
        if cloudfront and not domains:
            eh.add_log("Cloudfront requires at least one domain", {"cdef": cdef}, True)
            eh.perm_error("Cloudfront requires at least one domain", 0)
        if domains and len(domains.keys()) > 1 and not cloudfront:
            eh.add_log("Multiple domains requires cloudfront", {"cdef": cdef}, True)
            eh.perm_error("Multiple domains requires cloudfront", 0)

        index_document = cdef.get("index_document") or "index.html"
        error_document = cdef.get("error_document") or "index.html"
    
        op = event.get("op")

        if event.get("pass_back_data"):
            print(f"pass_back_data found")
        elif op == "upsert":
            if trust_level in ["full", "code"]:
                eh.add_op("compare_defs")
            else: #In case we want to change the trust level later
                eh.add_op("load_initial_props")
            
            eh.add_op("setup_codebuild_project")
            eh.add_op("setup_s3")
            eh.add_op("copy_output_to_s3")
            if cloudfront:
                eh.add_op("setup_cloudfront_oai")
                eh.add_op("setup_cloudfront_distribution")
                # eh.add_op("invalidate_files")
            else:
                eh.add_op("set_object_metadata")
            if cdef.get("config"):
                eh.add_op("add_config")
            if domains:
                eh.add_op("setup_route53", domains)

        elif op == "delete":
            eh.add_op("setup_codebuild_project")
            eh.add_op("setup_s3")
            eh.add_props(prev_state.get("props", {}))
            if cloudfront:
                eh.add_op("setup_cloudfront_oai")
                eh.add_op("setup_cloudfront_distribution")
            if domains:
                eh.add_op("setup_route53", domains)

        compare_defs(event)
        compare_etags(event, bucket, object_name, trust_level)

        load_initial_props(bucket, object_name)

        add_config(bucket, object_name, cdef.get("config"))
        if op == "upsert":
            setup_cloudfront_oai(cdef, oai_override_def, prev_state)
            setup_s3(cname, cdef, domains, index_document, error_document, prev_state, op)
        setup_codebuild_project(op, bucket, object_name, build_container_size, node_version, codebuild_project_override_def, trust_level)
        run_codebuild_build(codebuild_build_override_def, trust_level)
        # copy_output_to_s3(cloudfront, index_document, error_document)
        set_object_metadata(cdef, index_document, error_document, region, domains)
        setup_cloudfront_distribution(cname, cdef, domains, index_document, prev_state, cloudfront_distribution_override_def)
        
        #Have to do it after CF distribution is gone
        if event["op"] == "delete" and not eh.ops.get("setup_cloudfront_distribution") and not eh.state.get("completed_s3"):
            eh.add_op("setup_s3")
            setup_s3(cname, cdef, domains, index_document, error_document, prev_state, op)

        if op == "delete":
            setup_cloudfront_oai(cdef, oai_override_def, prev_state)
            setup_s3(cname, cdef, domains, index_document, error_document, prev_state, op)

        setup_route53(cdef, prev_state)
        # invalidate_files()
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
    old_rendef = event.get("prev_state", {}).get("rendef", {})
    new_rendef = event.get("component_def")

    _ = old_rendef.pop("trust_level", None)
    _ = new_rendef.pop("trust_level", None)

    if old_rendef == new_rendef:
        eh.add_op("compare_etags")

    else:
        eh.add_op("load_initial_props")
        eh.add_log("Definitions Don't Match, Deploying", {"old": old_rendef, "new": new_rendef})

@ext(handler=eh, op="compare_etags")
def compare_etags(event, bucket, object_name, trust_level):
    old_props = event.get("prev_state", {}).get("props", {})

    initial_etag = old_props.get("initial_etag")

    #Get new etag
    get_s3_etag(bucket, object_name)
    if eh.state.get("zip_etag"):
        new_etag = eh.state["zip_etag"]
        eh.add_props({"initial_etag": new_etag})
        if initial_etag == new_etag:
            if trust_level == "full":
                eh.add_log("Elevated Trust: No Change Detected", {"initial_etag": initial_etag, "new_etag": new_etag})
                eh.add_props(old_props)
                eh.add_links(event.get("prev_state", {}).get("links", {}))
                eh.add_state(event.get("prev_state", {}).get("state", {}))
                eh.declare_return(200, 100, success=True)
            else: #Code
                eh.add_log("Zipfile Unchanged, Skipping Build", {"initial_etag": initial_etag, "new_etag": new_etag})
                eh.add_props({
                    CODEBUILD_PROJECT_KEY: old_props.get(CODEBUILD_PROJECT_KEY),
                    CODEBUILD_BUILD_KEY: old_props.get(CODEBUILD_BUILD_KEY),
                })
                
                eh.complete_op("setup_codebuild_project")
                # eh.add_props({
                #     "codebuild_project_arn": old_props.get("codebuild_project_arn"),
                #     "codebuild_project_name": old_props.get("codebuild_project_name"),
                #     "hash": old_props.get("hash"),
                # })
                # eh.add_links({
                #     "Codebuild Project": gen_codebuild_link(old_props.get("codebuild_project_name"))
                # })

        else:
            eh.add_log("Code Changed, Deploying", {"old_etag": initial_etag, "new_etag": new_etag})

@ext(handler=eh, op="load_initial_props")
def load_initial_props(bucket, object_name):
    get_s3_etag(bucket, object_name)
    if eh.state.get("zip_etag"):
        eh.add_props({"initial_etag": eh.state.get("zip_etag")})

# def format_tags(tags_dict):
#     return [{"Key": k, "Value": v} for k,v in tags_dict]

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

@ext(handler=eh, op="setup_cloudfront_oai")
def setup_cloudfront_oai(cdef, oai_def, prev_state):
    print(f"props = {eh.props}")
    component_def = remove_none_attributes({
        "existing_id": cdef.get("oai_existing_id")
    })

    component_def.update(oai_def)

    function_arn = lambda_env('cloudfront_oai_extension_arn')

    if prev_state.get("props", {}).get(CLOUDFRONT_OAI_KEY, {}):
        eh.add_props({CLOUDFRONT_OAI_KEY: prev_state.get("props", {}).get(CLOUDFRONT_OAI_KEY, {})})


    proceed = eh.invoke_extension(
        arn=function_arn, component_def=component_def, 
        child_key=CLOUDFRONT_OAI_KEY, progress_start=85, progress_end=100,
        merge_props=False)
    print(f"proceed = {proceed}")

@ext(handler=eh, op="setup_s3", complete_op=False)
def setup_s3(cname, cdef, domains, index_document, error_document, prev_state, op):
    # Note that this function can be called multiple times if there are multiple domains
    # and there is no Cloudfront distribution

    # State 1: No Cloudfront distribution now.
    # State 2: No Cloudfront distribution before, but there is now.
    # State 3: Cloudfront distribution now and before.

    # If state 3, there will only be one S3 Key, so we should use it
    # If state 2, there may be multiple S3 Keys, we should just pick one that is currently being used
    # If state 1, there may be multiple S3 Keys, we need to do all the jumbling.
    if not eh.state.get("s3_domains"):
        if prev_state.get("props", {}).get(S3_KEY):
            prev_state['props'][f"{S3_KEY}_{SOLO_KEY}"] = prev_state['props'][S3_KEY]

        # If we used to have cloudfront
        if prev_state.get("props", {}).get(CLOUDFRONT_DISTRIBUTION_KEY):
            # Used to have Cloudfront. Only one S3 key
            old_s3_props = prev_state['props'].get(S3_KEY) or prev_state['props'].get(f"{S3_KEY}_{SOLO_KEY}")
            if domains and not cdef.get("cloudfront"):
                s3_domains = {SOLO_KEY: "delete", **domains}
                eh.state['s3_domains'] = s3_domains
            else:
                s3_domains = {SOLO_KEY: {"domain": old_s3_props.get("name")}}
                eh.state['s3_domains'] = s3_domains
            print(f"eh.state['s3_domains'] = {eh.state['s3_domains']}")
        else:
            prev_s3_state_keys = [k for k in prev_state.get("props", {}).keys() if k.startswith(f"{S3_KEY}_")]
            prev_s3_states = {k.replace(f"{S3_KEY}_", ""): prev_state["props"][k] for k in prev_s3_state_keys}
            print(f"prev_s3_states = {prev_s3_states}")
            # Did not previously have Cloudfront
            if cdef.get("cloudfront"):
                # Now have Cloudfront
                key_1 = list(domains.keys())[0]
                print(f"key_1 = {key_1}")
                s3_domains = {SOLO_KEY: {"domain": prev_s3_states.get(SOLO_KEY, {}).get("name")}}
                for k in prev_s3_states.keys():
                    if k != SOLO_KEY:
                        s3_domains[k] = "delete"
                print(f"s3_domains = {s3_domains}")
            else:
                # Still don't have Cloudfront
                s3_domains = domains or {SOLO_KEY: None}
                for k in prev_s3_states.keys():
                    if k not in s3_domains:
                        s3_domains[k] = "delete"
                print(f"s3_domains = {s3_domains}")
            
            eh.state["s3_domains"] = s3_domains

        # So that retries are handled properly
        eh.add_op("setup_s3", s3_domains)

    website_configuration = None
    block_public_access = True
    acl = None
    if cdef.get("cloudfront"):
        bucket_policy = {
            "Version": "2012-10-17",
            "Id": "BucketPolicyCloudfront",
            "Statement": [
                {
                    "Sid": "AllowCloudfront",
                    "Effect": "Allow",
                    "Principal": {
                        "AWS": eh.props[CLOUDFRONT_OAI_KEY]['arn']
                    },
                    "Action": "s3:GetObject",
                    "Resource": "$SELF$/*"
                }
            ]
        }
        allow_alternate_bucket_name = True

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
        allow_alternate_bucket_name = False

    function_arn = lambda_env('s3_extension_arn')
    delete_domain_key_list = [k for k in eh.state["s3_domains"].keys() if eh.state["s3_domains"][k] == "delete"]
    if delete_domain_key_list:
        child_key = f"{S3_KEY}_{delete_domain_key_list[0]}"
        domain_key = delete_domain_key_list[0]
        bucket_name = prev_state.get("props", {}).get(child_key, {}).get("name")
        op = "delete"

    else:
        domain_key = list(eh.state['s3_domains'].keys())[0]
        child_key = f"{S3_KEY}_{domain_key}"
        # Bucket name is the domain name, unless we are using Cloudfront
        # Really have 3 cases. If we have cloudfront, we use the last created bucket name
        # If we have a domain, we use the domain name
        # If we have neither, we should set it to None.
        previous_name = prev_state.get("props", {}).get(child_key, {}).get("name")
        domain_name = (eh.state["s3_domains"][domain_key] or {}).get("domain") #This will be none if there is no domain
        bucket_name = domain_name if (domain_name and not cloudfront) else (previous_name if previous_name else domain_name)

    component_def = remove_none_attributes({
        "name": bucket_name,
        "website_configuration": website_configuration,
        "bucket_policy": bucket_policy,
        "block_public_access": block_public_access,
        "acl": acl,
        "allow_alternate_bucket_name": allow_alternate_bucket_name,
        "tags": cdef.get("s3_tags")
    })
    # Not going to allow overrides at this time because we are doing this in dictionary format
    # component_def.update(s3_component_def)

    proceed = eh.invoke_extension(
        arn=function_arn, component_def=component_def, 
        child_key=child_key, progress_start=20, progress_end=50,
        links_prefix = f"{domain_key} " if domain_key != SOLO_KEY else "",
        merge_props=False, op=op)
    print(f"proceed = {proceed}")

    print(domain_key)
    print(child_key)
    if proceed:
        print(eh.state)
        _ = eh.state['s3_domains'].pop(domain_key)
        print(eh.state)
        if eh.state.get('s3_domains'):
            print(eh.state.get("s3_domains"))
            eh.add_op("setup_s3", s3_domains)
            setup_s3(cname, cdef, domains, index_document, error_document, prev_state, op)
        else:
            # This goes with the codebuild project changes.
            if not eh.state.get("destination_bucket_name"):
                if op == "upsert":
                    destination_bucket_name = list(map(lambda x: x['name'], [v for k, v in eh.props.items() if k.startswith(f"{S3_KEY}_")]))[0]
                else:
                    destination_bucket_name = bucket_name
                eh.add_state({"destination_bucket_name": destination_bucket_name})
            ##############################################
            eh.complete_op("setup_s3")
            eh.add_state({"completed_s3": True})

@ext(handler=eh, op="setup_codebuild_project")
def setup_codebuild_project(op, bucket, object_name, build_container_size, node_version, codebuild_def, trust_level):
    #This is a workaround for the builds being unreliable
    #We are just going to use 1 S3 bucket

    if not eh.state.get("codebuild_object_key"):
        eh.add_state({"codebuild_object_key": f"{random_id()}.zip"})

    if op == "upsert":
        component_def = {
            "s3_bucket": bucket,
            "s3_object": object_name,
            "build_container_size": build_container_size,
            "runtime_versions": {"nodejs": node_version},
            # "pre_build_commands": pre_build_commands,
            "build_commands": ["mkdir -p build", "npm install", "npm run build"],
            "buildspec_artifacts": {
                "files": [
                    "**/*"
                ],
                "base-directory": "build"
            },
            "artifacts": {
                "type": "S3",
                "location": eh.state['destination_bucket_name'],
                "path": "/", 
                "namespaceType": "NONE",
                "name": "/",
                "packaging": "NONE",
                "encryptionDisabled": True
            },
            "trust_level": trust_level
            # "post_build_commands": post_build_commands,
            # "privileged_mode": True
        }
    else:
        component_def = {
            "s3_bucket": bucket,
            "s3_object": object_name,
            "trust_level": trust_level
        }

    #Allows for custom overrides as the user sees fit
    component_def.update(codebuild_def)

    eh.invoke_extension(
        arn=lambda_env("codebuild_project_extension_arn"), 
        component_def=component_def, 
        child_key=CODEBUILD_PROJECT_KEY, progress_start=25, 
        progress_end=30
    )

    if op == "upsert":
        eh.add_op("run_codebuild_build")

# @ext(handler=eh, op="setup_codebuild_project")
# def setup_codebuild_project(op, bucket, object_name, build_container_size, node_version, codebuild_def, trust_level):
#     if not eh.state.get("codebuild_object_key"):
#         eh.add_state({"codebuild_object_key": f"{random_id()}.zip"})

#     component_def = {
#         "s3_bucket": bucket,
#         "s3_object": object_name,
#         "build_container_size": build_container_size,
#         "runtime_versions": {"nodejs": node_version},
#         # "pre_build_commands": pre_build_commands,
#         "build_commands": ["mkdir -p build", "npm install", "npm run build"],
#         "buildspec_artifacts": {
#             "files": [
#                 "**/*"
#             ],
#             "base-directory": "build"
#         },
#         "artifacts": {
#             "type": "S3",
#             "location": bucket,
#             "path": "/", 
#             "name": eh.state["codebuild_object_key"],
#             "packaging": "ZIP",
#             "encryptionDisabled": True
#         },
#         "trust_level": trust_level
#         # "post_build_commands": post_build_commands,
#         # "privileged_mode": True
#     }

#     #Allows for custom overrides as the user sees fit
#     component_def.update(codebuild_def)

#     eh.invoke_extension(
#         arn=lambda_env("codebuild_project_extension_arn"), 
#         component_def=component_def, 
#         child_key=CODEBUILD_PROJECT_KEY, progress_start=25, 
#         progress_end=30
#     )

#     if op == "upsert":
#         eh.add_op("run_codebuild_build")

@ext(handler=eh, op="run_codebuild_build")
def run_codebuild_build(codebuild_build_def, trust_level):
    print(eh.props)
    print(eh.links)

    component_def = {
        "project_name": eh.props[CODEBUILD_PROJECT_KEY]["name"],
        "trust_level": trust_level
    }

    component_def.update(codebuild_build_def)

    proceed = eh.invoke_extension(
        arn=lambda_env("codebuild_build_extension_arn"),
        component_def=component_def, 
        child_key=CODEBUILD_BUILD_KEY, progress_start=30, 
        progress_end=45
    )

    if proceed:
        eh.add_op("copy_output_to_s3")
    # eh.add_op("get_final_props")


# @ext(handler=eh, op="copy_output_to_s3")
# def copy_output_to_s3(cloudfront, index_document, error_document):
#     if cloudfront:
#         eh.state['s3_destination_folder'] = str(current_epoch_time_usec_num())

#     # If cloudfront, we want to copy it to the cloudfront bucket inside a folder.
#     # Otherwise, we copy it to the root of the bucket so Route53 serves it directly.
#     s3_bucket_names = list(map(lambda x: x['name'], [v for k, v in eh.props.items() if k.startswith(f"{S3_KEY}_")]))
#     if not s3_bucket_names:
#         eh.perm_error("No S3 Buckets to copy to", 50)
#         eh.add_log("No S3 Buckets. Shouldn't Happen", {"props": eh.props}, is_error=True)
#         return 0

#     codebuild_project_props = eh.props[CODEBUILD_PROJECT_KEY]
#     build_bucket = codebuild_project_props["zip_artifact_bucket"]
#     build_key = codebuild_project_props["zip_artifact_key"]

#     # Download the zip file from S3
#     obj = s3.get_object(Bucket=build_bucket, Key=build_key)
#     zipfile_bytes = io.BytesIO(obj['Body'].read())

#     tmp_directory = f"/tmp/{random_id()}"
#     os.mkdir(tmp_directory)
#     print(tmp_directory)
    
#     # Extract the contents of the zip file
#     with zipfile.ZipFile(zipfile_bytes, 'r') as zip_ref:
#         zip_ref.extractall(tmp_directory)
#         print(os.listdir(tmp_directory))
#         print(zip_ref.namelist())
#         # Upload the extracted files to S3
#         for file_name in zip_ref.namelist():
#             key = f"{eh.state.get('s3_destination_folder')}/{file_name}" if eh.state.get("s3_destination_folder") else file_name
#             for s3_bucket_name in s3_bucket_names:
#                 file_bytes = open(f"{tmp_directory}/{file_name}", 'rb')
#                 content_type = mimetypes.guess_type(file_name)[0] or 'binary/octet-stream'
#                 cache_control = None
#                 if file_name.endswith("json"):
#                     content_type = "binary/octet-stream"
#                 elif file_name.endswith(".js"):
#                     content_type = "application/x-javascript"
#                 if file_name in [index_document, error_document]:
#                     cache_control = "max-age=0"

#                 print(f"Uploading {file_name} with content type {content_type}")

#                 put_object_args = remove_none_attributes({
#                     "Bucket": s3_bucket_name,
#                     "Key": key,
#                     "Body": file_bytes,
#                     "ContentType": content_type,
#                     "CacheControl": cache_control
#                 })

#                 s3.put_object(**put_object_args)
#                 file_bytes.close()

@ext(handler=eh, op="set_object_metadata")
def set_object_metadata(cdef, index_document, error_document, region, domains):
    bucket_name = eh.state["destination_bucket_name"]

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


        if (not cdef.get("cloudfront")) and (not domains):
            eh.add_links({"Website URL": gen_s3_url(bucket_name, "/", region)})
    except botocore.exceptions.ClientError as e:
        eh.add_log("Error setting Object Metadata", {"error": str(e)}, True)
        eh.retry_error(str(e), 95 if not domains else 85)

# Only runs when cloudfront is off
# @ext(handler=eh, op="set_object_metadata")
# def set_object_metadata(cdef, index_document, error_document, region, domains):

#     s3_bucket_names = list(map(lambda x: x['name'], [v for k, v in eh.props.items() if k.startswith(f"{S3_KEY}_")]))
#     for bucket_name in s3_bucket_names:
#         key = index_document
#         print(f"bucket_name = {bucket_name}")
#         print(f"key = {key}")
#         # print(f"s3_url_path = {s3_url_path}")

#         try:
#             response = s3.copy_object(
#                 Bucket=bucket_name,
#                 Key=key,
#                 CopySource=f"{bucket_name}/{key}",
#                 MetadataDirective="REPLACE",
#                 CacheControl="max-age=0",
#                 ContentType="text/html"
#             )
#             eh.add_log(f"Fixed {index_document}", response)

#             if error_document != index_document:
#                 key = error_document
#                 response = s3.copy_object(
#                     Bucket=bucket_name,
#                     Key=key,
#                     CopySource=f"{bucket_name}/{key}",
#                     MetadataDirective="REPLACE",
#                     CacheControl="max-age=0",
#                     ContentType="text/html"
#                 )
#                 eh.add_log(f"Fixed {error_document}", response)


#             if (not cdef.get("cloudfront")) and (not domains):
#                 eh.add_links({"Website URL": gen_s3_url(bucket_name, "/", region)})
#         except botocore.exceptions.ClientError as e:
#             eh.add_log("Error setting Object Metadata", {"error": str(e)}, True)
#             eh.retry_error(str(e), 95 if not domains else 85)

@ext(handler=eh, op="setup_cloudfront_distribution")
def setup_cloudfront_distribution(cname, cdef, domains, index_document, prev_state, cloudfront_distribution_override_def):
    print(f"props = {eh.props}")

    S3 = eh.props.get(f"{S3_KEY}_{SOLO_KEY}", {})
    bucket_names = list(map(lambda x: x['name'], [v for k, v in eh.props.items() if k.startswith(f"{S3_KEY}_")]))
    component_def = remove_none_attributes({
        "aliases": list(set(map(lambda x: x['domain'], domains.values()))),
        # "target_s3_bucket": S3.get("name"),
        # "default_root_object": index_document if not eh.state.get("s3_destination_folder") else f"{eh.state.get('s3_destination_folder')}/{index_document}",
        "target_s3_bucket": eh.state.get("destination_bucket_name") or bucket_names[0],
        "default_root_object": index_document,
        "oai_id": eh.props.get(CLOUDFRONT_OAI_KEY, {}).get("id"),
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

    if prev_state.get("props", {}).get(CLOUDFRONT_DISTRIBUTION_KEY, {}):
        eh.add_props({CLOUDFRONT_DISTRIBUTION_KEY: prev_state.get("props", {}).get(CLOUDFRONT_DISTRIBUTION_KEY, {})})

    component_def.update(cloudfront_distribution_override_def)

    function_arn = lambda_env('cloudfront_distribution_extension_arn')

    proceed = eh.invoke_extension(
        arn=function_arn, component_def=component_def, 
        child_key=CLOUDFRONT_DISTRIBUTION_KEY, progress_start=65, progress_end=85,
        merge_props=False)
    print(f"proceed = {proceed}")
        


@ext(handler=eh, op="setup_route53", complete_op=False)
def setup_route53(cdef, prev_state, i=1):
    print(f"props = {eh.props}")
    available_domains = eh.ops["setup_route53"]
    domain_key = list(available_domains.keys())[0]
    domain = available_domains[domain_key].get("domain")
    if not domain:
        eh.perm_error("'domains' dictionary must contain a 'domain' key inside the domain key")
        return
    if cdef.get("cloudfront"):
        component_def = remove_none_attributes({
            "domain": domain,
            "route53_hosted_zone_id": available_domains[domain_key].get("hosted_zone_id"),
            "alias_target_type": "cloudfront",
            "target_cloudfront_domain_name": eh.props["Distribution"]["domain_name"]
        })
    else:
        S3 = eh.props.get(S3_KEY, {})
        component_def = {
            "target_s3_region": S3.get("region"),
            "target_s3_bucket": S3.get("name")
        }

    function_arn = lambda_env('route53_extension_arn')
    
    child_key = f"Route53_{domain_key}"

    if prev_state and prev_state.get("props", {}).get(child_key, {}):
        eh.add_props({child_key: prev_state.get("props", {}).get(child_key, {})})

    proceed = eh.invoke_extension(
        arn=function_arn, component_def=component_def, links_prefix=f"{domain_key} ",
        child_key=child_key, progress_start=85, progress_end=100
    )

    if proceed:
        link_name = f"{domain_key} Website URL" 
        # if (i != 1) or (len(list(available_domains.keys())) > 1) else "Website URL"
        eh.add_links({link_name: f'http://{eh.props[child_key].get("domain")}'})
        _ = available_domains.pop(domain_key)
        if available_domains:
            eh.add_op("setup_route53", available_domains)
            setup_route53(cdef, prev_state, i=i+1)
        else:
            eh.complete_op("setup_route53")

#Note that invalidate files and checking for it should really be its own plugin.
#Not doing this atm, because cloudfront will be changing its root object.
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

def gen_s3_url(bucket_name, s3_url_path, region):
    return f'http://{bucket_name}.s3-website-{region}.amazonaws.com{s3_url_path if s3_url_path != "/" else ""}'

def gen_codebuild_link(codebuild_project_name):
    return f"https://console.aws.amazon.com/codesuite/codebuild/projects/{codebuild_project_name}"

def form_domain(bucket, base_domain):
    if bucket and base_domain:
        return f"{bucket}.{base_domain}"
    else:
        return None

def fix_domains(domains):
    if domains:
        retval = {}
        for domain_key, domain in domains.items():
            if isinstance(domain, str):
                retval[domain_key] = {"domain": domain}
            else:
                retval[domain_key] = domain
        return retval

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

# @ext(handler=eh, op="setup_status_objects")
# def setup_status_objects(bucket):
#     s3 = boto3.client("s3")
#     print(f"setup_status_objects")

#     try:
#         response = s3.get_object(Bucket=bucket, Key=ERROR_FILE)
#         eh.add_log("Status Objects Exist", {"bucket": bucket, "success": SUCCESS_FILE, "error": ERROR_FILE})
#     except botocore.exceptions.ClientError as e:
#         if e.response['Error']['Code'] == "NoSuchKey":
#             try:
#                 success = {"value": "success"}
#                 s3.put_object(
#                     Body=json.dumps(success),
#                     Bucket=bucket,
#                     Key=SUCCESS_FILE
#                 )

#                 error = {"value": "error"}
#                 s3.put_object(
#                     Body=json.dumps(error),
#                     Bucket=bucket,
#                     Key=ERROR_FILE
#                 )
        
#                 eh.add_log("Status Objects Created", {"bucket": bucket, "success": SUCCESS_FILE, "error": ERROR_FILE})
#             except:
#                 eh.add_log("Error Writing Status Objects", {"error": str(e)}, True)
#                 eh.retry_error(str(e), 10)

#         else:
#             eh.add_log("Error Getting Status Object", {"error": str(e)}, True)
#             eh.retry_error(str(e), 10)




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


# CODEBUILD_RUNTIME_TO_IMAGE_MAPPING = {
#     "android28": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
#     "android29": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
#     "dotnet3.1": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
#     "dotnet5.0": "aws/codebuild/standard:5.0",
#     "dotnet6.0": "aws/codebuild/standard:6.0",
#     "golang1.12": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
#     "golang1.13": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
#     "golang1.14": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
#     "golang1.15": "aws/codebuild/standard:5.0",
#     "golang1.16": "aws/codebuild/standard:5.0",
#     "golang1.18": "aws/codebuild/amazonlinux2-x86_64-standard:4.0",
#     "javacorretto8": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
#     "javacorretto11": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
#     "javacorretto17": "aws/codebuild/amazonlinux2-x86_64-standard:4.0",
#     "nodejs8": "aws/codebuild/amazonlinux2-aarch64-standard:1.0",
#     "nodejs10": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
#     "nodejs12": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
#     "nodejs14": "aws/codebuild/standard:5.0",
#     "nodejs16": "aws/codebuild/amazonlinux2-x86_64-standard:4.0",
#     "php7.3": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
#     "php7.4": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
#     "php8.0": "aws/codebuild/standard:5.0",
#     "php8.1": "aws/codebuild/amazonlinux2-x86_64-standard:4.0",
#     "python3.7": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
#     "python3.8": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
#     "python3.9": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
#     "python3.10": "aws/codebuild/standard:6.0",
#     "ruby2.6": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
#     "ruby2.7": "aws/codebuild/amazonlinux2-x86_64-standard:3.0",
#     "ruby3.1": "aws/codebuild/amazonlinux2-x86_64-standard:4.0",
# }



