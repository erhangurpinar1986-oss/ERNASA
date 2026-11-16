SET LOCAL search_path = ernasa_control, pg_catalog;

CREATE FUNCTION enforce_company_identity() RETURNS trigger
LANGUAGE plpgsql SET search_path = ernasa_control, pg_catalog AS $$
BEGIN
    RAISE EXCEPTION 'Company identity is immutable' USING ERRCODE = '23514';
END;
$$;
CREATE TRIGGER immutable_company_id BEFORE UPDATE OF id ON companies
FOR EACH ROW WHEN (OLD.id IS DISTINCT FROM NEW.id) EXECUTE FUNCTION enforce_company_identity();
CREATE TRIGGER immutable_company_code BEFORE UPDATE OF code ON company_profile
FOR EACH ROW WHEN (OLD.code IS DISTINCT FROM NEW.code) EXECUTE FUNCTION enforce_company_identity();

CREATE FUNCTION seed_control_metadata() RETURNS trigger
LANGUAGE plpgsql SET search_path = ernasa_control, pg_catalog AS $$
BEGIN
    IF TG_TABLE_NAME = 'companies' THEN
        INSERT INTO company_profile(company_id, code)
        VALUES (NEW.id, 'ERN-' || repeat('0', greatest(0, 8-length(NEW.id::text))) || NEW.id::text);
    ELSE
        INSERT INTO ik_personnel_details(user_id, created_at)
        VALUES (NEW.id, CURRENT_TIMESTAMP::text);
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER company_profile_insert AFTER INSERT ON companies
FOR EACH ROW EXECUTE FUNCTION seed_control_metadata();
CREATE TRIGGER ik_user_created AFTER INSERT ON users
FOR EACH ROW EXECUTE FUNCTION seed_control_metadata();

CREATE FUNCTION rotate_portal_version() RETURNS trigger
LANGUAGE plpgsql SET search_path = ernasa_control, pg_catalog AS $$
DECLARE tenant bigint;
BEGIN
    IF TG_TABLE_NAME = 'companies' THEN tenant := NEW.id;
    ELSE tenant := NEW.company_id;
    END IF;
    UPDATE company_portals SET version = replace(gen_random_uuid()::text, '-', '')
    WHERE company_id = tenant;
    RETURN NEW;
END;
$$;
CREATE TRIGGER ats_subscription_insert AFTER INSERT ON ats_subscriptions
FOR EACH ROW EXECUTE FUNCTION rotate_portal_version();
CREATE TRIGGER ats_subscription_update AFTER UPDATE OF status, starts, ends ON ats_subscriptions
FOR EACH ROW WHEN ((OLD.status, OLD.starts, OLD.ends) IS DISTINCT FROM (NEW.status, NEW.starts, NEW.ends))
EXECUTE FUNCTION rotate_portal_version();
CREATE TRIGGER ik_company_change AFTER UPDATE OF status ON companies
FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status) EXECUTE FUNCTION rotate_portal_version();
CREATE TRIGGER ik_subscription_change AFTER UPDATE ON subscriptions
FOR EACH ROW WHEN ((OLD.status, OLD.starts, OLD.ends) IS DISTINCT FROM (NEW.status, NEW.starts, NEW.ends))
EXECUTE FUNCTION rotate_portal_version();
CREATE TRIGGER ik_product_change AFTER UPDATE OF product ON service_terms
FOR EACH ROW WHEN (OLD.product IS DISTINCT FROM NEW.product) EXECUTE FUNCTION rotate_portal_version();

CREATE FUNCTION rotate_user_version() RETURNS trigger
LANGUAGE plpgsql SET search_path = ernasa_control, pg_catalog AS $$
DECLARE uid bigint;
BEGIN
    IF TG_TABLE_NAME = 'users' THEN uid := NEW.id;
    ELSE uid := NEW.user_id;
    END IF;
    UPDATE ik_user_roles SET version = replace(gen_random_uuid()::text, '-', '') WHERE user_id = uid;
    RETURN NEW;
END;
$$;
CREATE TRIGGER ik_user_change AFTER UPDATE ON users
FOR EACH ROW WHEN ((OLD.active, OLD.secret, OLD.role, OLD.company_id)
    IS DISTINCT FROM (NEW.active, NEW.secret, NEW.role, NEW.company_id))
EXECUTE FUNCTION rotate_user_version();
CREATE TRIGGER ik_role_change AFTER UPDATE OF role ON ik_user_roles
FOR EACH ROW WHEN (OLD.role IS DISTINCT FROM NEW.role) EXECUTE FUNCTION rotate_user_version();
